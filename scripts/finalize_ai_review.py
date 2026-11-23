"""Publish an explicitly AI-assisted release review without creating human labels."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from agentreplay.canonical import canonical_json
from agentreplay.evals.harness import git_revision
from agentreplay.evals.labeling import blind_run
from agentreplay.evals.stats import manual_scores, rate
from agentreplay.evals.tasks import load_tasks
from agentreplay.evals.verifiers import verify
from agentreplay.report import build_report, render_markdown
from agentreplay.store import TraceStore

REVIEW_TYPE = "ai_assisted_not_human"
KINDS = {"loop", "invalid_tool_call", "timeout"}


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_task_review(path: Path) -> dict[str, Any]:
    evidence = json.loads(path.read_text())
    if (
        evidence.get("review_type") != REVIEW_TYPE
        or evidence.get("reviewer") != "codex-ai"
    ):
        raise ValueError("task evidence must identify the AI reviewer")
    root = Path(__file__).resolve().parents[1]
    data = root / "src/agentreplay/evals/data/tasks.jsonl"
    if evidence.get("tasks_version_sha256") != file_sha(data):
        raise ValueError("task evidence belongs to a different frozen task version")
    review = (data.parent.parent / "REVIEW.md").read_text()
    selected = set(re.findall(r"^\| ([a-z]+-\d{3}) \|", review, re.MULTILINE))
    rows = evidence["rows"]
    if (
        len(selected) != 20
        or len(rows) != 20
        or {row["task_id"] for row in rows} != selected
    ):
        raise ValueError("task review must cover the fixed 20-task sample exactly once")
    tasks = {task.id: task for task in load_tasks()}
    checked = []
    for row in rows:
        task = tasks[row["task_id"]]
        proposed = (
            row["final_state"]
            if task.verifier.name == "exact_state"
            else json.loads(row["final_answer"])[
                "codes" if task.env == "search" else "result"
            ]
        )
        expected = task.verifier.params["expected"]
        # Numeric task goals accept equivalent integers/floats; trace hashes do not.
        correct = (
            proposed == expected
            if task.verifier.name == "numeric_answer"
            else canonical_json(proposed) == canonical_json(expected)
        )
        accepted = verify(task, row["final_state"], row["final_answer"])
        if (
            not row.get("reasoning")
            or row.get("solvable") is not True
            or not correct
            or not accepted
        ):
            raise ValueError(f"AI task review needs adjudication: {task.id}")
        checked.append(
            {"task_id": task.id, "solvable": True, "verifier_matches_solution": True}
        )
    return {
        "review_type": REVIEW_TYPE,
        "reviewer": "codex-ai",
        "n": 20,
        "verified_solutions": rate(len(checked), 20),
        "tasks": checked,
        "evidence_sha256": file_sha(path),
    }


def check_label_review(
    store: TraceStore, path: Path, sample_path: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evidence = json.loads(path.read_text())
    sample = json.loads(sample_path.read_text())
    rows = evidence["rows"]
    ids = {item["run_id"] for item in sample}
    if (
        evidence.get("review_type") != REVIEW_TYPE
        or evidence.get("reviewer") != "codex-ai"
        or evidence.get("sample_sha256") != file_sha(sample_path)
    ):
        raise ValueError(
            "label evidence must identify the AI reviewer and fixed sample"
        )
    if (
        len(sample) != 100
        or len(ids) != 100
        or len(rows) != 100
        or {row["run_id"] for row in rows} != ids
    ):
        raise ValueError("AI labels must cover 100 unique sampled runs exactly once")
    expected = {item["run_id"]: item for item in sample}
    records = []
    for row in rows:
        item = expected[row["run_id"]]
        run = store.get_run(row["run_id"])
        if (
            row.get("reviewer") != "codex-ai"
            or not row.get("rationale")
            or row["task_id"] != item["task_id"]
            or row["env"] != item["env"]
            or row["trial"] != item["trial"]
            or run is None
            or run.kind != "live"
            or run.task_id != item["task_id"]
            or run.trial != item["trial"]
            or run.config.get("phase") != "record"
            or run.config.get("evaluation_id") != evidence["source_evaluation_id"]
        ):
            raise ValueError(
                "AI label evidence does not match its original natural run"
            )
        labels = row["labels"]
        if set(labels) != KINDS or any(
            type(value) is not bool for value in labels.values()
        ):
            raise ValueError(
                "each AI judgment needs exactly three boolean presence labels"
            )
        transcript = json.dumps(blind_run(store, row["run_id"]), indent=2) + "\n"
        digest = hashlib.sha256(transcript.encode()).hexdigest()
        if digest != row["blind_transcript_sha256"]:
            raise ValueError(
                "AI judgment transcript hash does not match the original run"
            )
        records.append(
            {
                "run_id": run.id,
                "labels": [
                    {"kind": kind, "present": present}
                    for kind, present in labels.items()
                ],
            }
        )
    for env in {item["env"] for item in sample}:
        if sum(item["env"] == env for item in sample) != 20:
            raise ValueError("AI sample must have 20 runs per environment")
    # Compare only after every frozen judgment and original transcript was validated.
    for record in records:
        record["findings"] = [
            finding.model_dump() for finding in store.findings(record["run_id"])
        ]
    return records, evidence


def release_report(
    store: TraceStore, task_path: Path, label_path: Path, sample_path: Path
) -> dict[str, Any]:
    task_review = check_task_review(task_path)
    records, evidence = check_label_review(store, label_path, sample_path)
    report = build_report(store, evidence["source_evaluation_id"])
    report["config"]["release_review"] = {
        "status": "complete_under_owner_authorized_ai_assisted_scope",
        "human_validation": "not_performed",
        "reviewer": "codex-ai",
        "report_code_sha": git_revision(),
        "task_review": task_review,
        "natural_label_evidence_sha256": file_sha(label_path),
        "judgment_date": evidence["date"],
        "limitations": evidence["limitations"],
    }
    report["notes"] = [
        note.replace(
            "Natural tool failures currently gain",
            "The original M9 runner let natural tool failures gain",
        )
        for note in report["notes"]
    ]
    report["metrics"]["ai_assisted"] = manual_scores(records)
    report["metrics"]["ai_assisted"]["unit"] = (
        "AI presence judgments on original natural runs; not manual human ground truth"
    )
    report["notes"] += [
        "Owner authorized AI-assisted replacement of the planned human review on 2026-11-23; 20 task solutions and 100 natural-run judgments are complete under that amended scope.",
        "Original owner_review=pending metadata is retained as recording-time provenance; no independent human review was performed and no validated benchmark or significance claim is made.",
        "AI judgments are separate from manual human scores and eval_labels; the same AI implemented detectors and knew prior aggregate results, so this is not independent or fully blinded validation.",
        "The AI sample contains no positive loop/invalid-call/timeout judgments: natural-positive sensitivity and precision remain undefined, not 100%.",
        "Metrics above retain original M9 code and observations. Post-fix source fidelity and loop results are separately generated in reports/release-verification.json.",
    ]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--tasks", type=Path, default=Path("reviews/ai-task-solutions.json")
    )
    parser.add_argument(
        "--labels", type=Path, default=Path("reviews/ai-natural-labels.json")
    )
    parser.add_argument(
        "--sample", type=Path, default=Path("reports/live/manual-sample.json")
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.db.is_file() or args.out.exists():
        parser.error("database must exist and output directory must be new")
    store = TraceStore(args.db)
    try:
        report = release_report(store, args.tasks, args.labels, args.sample)
    finally:
        store.close()
    markdown = render_markdown(report)
    args.out.mkdir(parents=True)
    (args.out / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    (args.out / "summary.md").write_text(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
