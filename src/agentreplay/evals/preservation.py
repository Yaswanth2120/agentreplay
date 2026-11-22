"""Consistent database snapshots, real recorded goldens, and verifiable manifests."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

from ..exportio import export_trace
from ..report import write_report
from ..store import TraceStore
from .resume import effective_runs
from .tasks import DATA


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def preserve(store: TraceStore, out: str | Path, evaluation_id: str) -> dict[str, Any]:
    folder = Path(out)
    if folder.exists() or folder.with_suffix(".tar.gz").exists():
        raise FileExistsError("backup output already exists; choose a new path")
    runs = [
        run
        for run in store.all_runs()
        if run.config.get("evaluation_id") == evaluation_id
    ]
    if not runs:
        raise ValueError("unknown evaluation to preserve")
    folder.mkdir(parents=True)
    store.backup(folder / "results.db")
    snapshot = TraceStore(folder / "results.db")
    try:
        runs = [
            run
            for run in snapshot.all_runs()
            if run.config.get("evaluation_id") == evaluation_id
        ]
        goldens = folder / "goldens"
        goldens.mkdir()
        for run in effective_runs(runs):
            if (
                run.config["phase"] == "record"
                and run.finished_at is not None
                and snapshot.get_evaluation(run.id) is not None
            ):
                export_trace(
                    snapshot,
                    run.trace_id,
                    goldens / f"{run.task_id}-{run.agent_version}-{run.trial}.jsonl",
                )
        for trace in snapshot.list_traces():
            if trace.name == "vllm-stream-smoke" and any(
                run.task_success is True for run in snapshot.runs(trace.id)
            ):
                export_trace(snapshot, trace.id, goldens / f"smoke-{trace.id}.jsonl")
        try:
            write_report(snapshot, folder / "reports", evaluation_id)
            report_status = "complete"
        except ValueError as error:
            report_status = f"incomplete: {error}"
        (folder / "evaluation-config.json").write_text(
            json.dumps(runs[0].config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    finally:
        snapshot.close()
    (folder / "session.json").write_text(
        json.dumps(
            {
                "evaluation_id": evaluation_id,
                "database": str((folder / "results.db").resolve()),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    shutil.copy2(DATA / "tasks.jsonl", folder / "tasks.jsonl")
    shutil.copy2(DATA / "corpus.jsonl", folder / "corpus.jsonl")
    shutil.copy2(DATA.parent / "REVIEW.md", folder / "REVIEW.md")
    manifest = {
        "evaluation_id": evaluation_id,
        "report_status": report_status,
        "files": {
            str(path.relative_to(folder)): file_sha256(path)
            for path in sorted(folder.rglob("*"))
            if path.is_file()
        },
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    verify_backup(folder)
    archive = folder.with_suffix(".tar.gz")
    with tarfile.open(archive, "x:gz") as handle:
        handle.add(folder, arcname=folder.name)
    return {
        "directory": str(folder),
        "archive": str(archive),
        "sha256": file_sha256(archive),
        "evaluation_id": evaluation_id,
        "report_status": report_status,
    }


def verify_backup(folder: str | Path) -> dict[str, Any]:
    root = Path(folder)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        path = root / name
        if (
            Path(name).is_absolute()
            or ".." in Path(name).parts
            or path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
        ):
            raise ValueError("invalid backup manifest path")
        if file_sha256(path) != digest:
            raise ValueError(f"backup checksum mismatch: {name}")
    if "results.db" not in manifest["files"]:
        raise ValueError("backup has no database")
    return manifest
