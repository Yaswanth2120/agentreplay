"""Owner-run live preparation, version capture, resumable execution, and export."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import importlib.metadata
import json
import os
import platform
import random
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..exportio import export_trace
from ..proxy import create_app
from ..schema import EvaluationConfig
from ..store import TraceStore
from ..streaming import reassemble
from .envs.calc import Calculator
from .harness import evaluate, git_revision, load_config
from .preservation import preserve, verify_backup
from .resume import effective_runs
from .tasks import load_tasks

ALLOWED_FLAGS = {
    "--host",
    "--port",
    "--enable-auto-tool-choice",
    "--tool-call-parser",
    "--max-model-len",
    "--max-num-seqs",
    "--dtype",
    "--generation-config",
    "--revision",
    "--tokenizer-revision",
    "--gpu-memory-utilization",
    "--tensor-parallel-size",
}


def server_command(config: EvaluationConfig) -> list[str]:
    if (
        config.backend != "live"
        or not config.model_revision
        or not re.fullmatch(r"[0-9a-f]{40}", config.model_revision)
    ):
        raise ValueError("live server requires a pinned 40-character model revision")
    values: dict[str, str | bool] = {}
    args = iter(config.vllm_flags)
    for flag in args:
        if flag not in ALLOWED_FLAGS or flag in values:
            raise ValueError(f"unsupported or duplicate server flag {flag!r}")
        value = True if flag == "--enable-auto-tool-choice" else next(args, None)
        if value is None or (isinstance(value, str) and value.startswith("--")):
            raise ValueError(f"missing value for {flag}")
        values[flag] = value
    parsed = urlparse(config.upstream_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("live runner expects a credential-free loopback upstream")
    expected = {
        "--host": "127.0.0.1",
        "--port": str(parsed.port or 80),
        "--enable-auto-tool-choice": True,
        "--tool-call-parser": "hermes",
        "--revision": config.model_revision,
        "--tokenizer-revision": config.model_revision,
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "server flags must match loopback address, Hermes tools, and pinned model/tokenizer revisions"
        )
    return [
        sys.executable,
        "-m",
        "vllm.entrypoints.cli.main",
        "serve",
        config.model,
        *config.vllm_flags,
    ]


def collect_environment(config: EvaluationConfig) -> dict[str, Any]:
    packages = {
        dist.metadata["Name"]: dist.version
        for dist in importlib.metadata.distributions()
        if dist.metadata.get("Name")
    }
    normalized = {
        name.lower().replace("_", "-"): value for name, value in packages.items()
    }
    if normalized.get("vllm") != config.vllm_version:
        raise ValueError(
            f"install the configured vLLM {config.vllm_version} in this Python environment first"
        )
    gpu = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total,uuid",
            "--format=csv,noheader",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    cuda = subprocess.run(
        [sys.executable, "-c", "import torch; print(torch.version.cuda)"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": packages,
        "gpu_inventory": gpu.stdout.strip().splitlines(),
        "torch_cuda": cuda.stdout.strip(),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "git_sha": git_revision(),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def serve(config: EvaluationConfig, receipt_path: Path) -> int:
    command = server_command(config)
    if receipt_path.exists():
        old = json.loads(receipt_path.read_text(encoding="utf-8"))
        if old.get("status") == "running":
            try:
                os.kill(old["pid"], 0)
            except ProcessLookupError:
                pass
            else:
                raise ValueError("a server from this receipt is already running")
    environment = collect_environment(config)
    process = subprocess.Popen(command)
    receipt = {
        "pid": process.pid,
        "command": command,
        "environment": environment,
        "status": "running",
    }
    try:
        write_json(receipt_path, receipt)
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        write_json(
            receipt_path,
            {**receipt, "status": "exited", "returncode": process.returncode},
        )


def validate_receipt(config: EvaluationConfig, path: Path) -> dict[str, Any]:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt["status"] != "running" or receipt["command"] != server_command(config):
        raise ValueError("server receipt does not match the configured running command")
    os.kill(receipt["pid"], 0)
    cmdline = Path(f"/proc/{receipt['pid']}/cmdline").read_bytes().decode().split("\0")
    if cmdline[:-1] != receipt["command"]:
        raise ValueError("server PID no longer matches its captured command")
    packages = {
        name.lower().replace("_", "-"): version
        for name, version in receipt["environment"]["packages"].items()
    }
    if packages.get("vllm") != config.vllm_version:
        raise ValueError("server vLLM version differs from live configuration")
    return receipt


async def capture_smoke(
    store: TraceStore,
    config: EvaluationConfig,
    out: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    calculator = Calculator()
    request = {
        "model": config.model,
        "messages": [
            {
                "role": "user",
                "content": "Use calculate with op add, a 2, b 3. Call the tool; do not answer in text.",
            }
        ],
        "tools": calculator.tools()[:1],
        "stream": True,
        "temperature": 0.7,
        "seed": 0,
        "max_tokens": config.max_tokens,
        "chat_template_kwargs": config.chat_template_kwargs,
    }
    app = create_app(
        store,
        "live",
        trace_name="vllm-stream-smoke",
        upstream_url=config.upstream_url,
        transport=transport,
        run_config={
            "model": config.model,
            "vllm_version": config.vllm_version,
            "vllm_flags": config.vllm_flags,
        },
    )
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://smoke-proxy"
            ) as client,
        ):
            response = await client.post("/v1/chat/completions", json=request)
            response.raise_for_status()
        exchanges = store.exchanges(app.state.trace_id)
        exchange = exchanges[0]
        calls = exchange.tool_calls()
        if (
            not exchange.chunks
            or reassemble(exchange.chunks) != exchange.response
            or len(calls) != 1
            or calls[0].name != "calculate"
        ):
            app.state.runs.finish(
                app.state.run_id,
                "agent_error",
                detail="real streaming tool smoke failed",
            )
            raise ValueError(
                "stream/tool parser smoke failed; inspect captured trace before the full evaluation"
            )
        from ..json_validation import parse_arguments
        from ..tools import ToolRecorder

        args = parse_arguments(calls[0].arguments)
        if args != {"op": "add", "a": 2, "b": 3}:
            app.state.runs.finish(
                app.state.run_id,
                "agent_error",
                detail="unexpected smoke tool arguments",
            )
            raise ValueError("smoke model did not emit the requested addition")
        recorder = ToolRecorder(
            store,
            app.state.trace_id,
            "record",
            faults=app.state.runs.faults(app.state.run_id),
        )
        recorder.call("calculate", args, calculator.calculate)
        app.state.runs.finish(app.state.run_id, "completed", task_success=True)
        export_trace(store, app.state.trace_id, out)
        return app.state.trace_id
    finally:
        if store.get_run(app.state.run_id).finished_at is None:
            app.state.runs.finish(
                app.state.run_id,
                "agent_error",
                detail="stream/tool smoke did not finish",
            )


def sample_labels(
    store: TraceStore, evaluation_id: str, *, seed: int = 1234, per_env: int = 20
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    runs = effective_runs(
        [
            run
            for run in store.all_runs()
            if run.config.get("evaluation_id") == evaluation_id
        ]
    )
    selected = []
    for name in ("calc", "kv", "files", "search", "tickets"):
        candidates = [
            run
            for run in runs
            if run.kind == "live"
            and run.config["phase"] == "record"
            and run.config["task"]["env"] == name
            and run.finished_at is not None
            and store.get_evaluation(run.id) is not None
        ]
        if len(candidates) < per_env:
            raise ValueError(f"not enough natural {name} runs for manual labeling")
        selected.extend(
            {"run_id": run.id, "env": name, "task_id": run.task_id, "trial": run.trial}
            for run in rng.sample(candidates, per_env)
        )
    return selected


async def live_run(
    store: TraceStore,
    config: EvaluationConfig,
    state_path: Path,
    receipt: dict[str, Any],
    smoke_path: Path,
) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=config.upstream_url, timeout=30) as client:
        response = await client.get("/v1/models")
        response.raise_for_status()
        if config.model not in {item["id"] for item in response.json()["data"]}:
            raise ValueError("server does not advertise the configured model")
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state["database"] != str(Path(store.database_path()).resolve()):
            if not any(
                run.config.get("evaluation_id") == state["evaluation_id"]
                for run in store.all_runs()
            ):
                raise ValueError("session file belongs to a different database")
            state["database"] = str(Path(store.database_path()).resolve())
            write_json(state_path, state)
    else:
        state = {
            "evaluation_id": uuid.uuid4().hex,
            "database": str(Path(store.database_path()).resolve()),
        }
        write_json(state_path, state)
    smoke_path = smoke_path.with_name(f"{state['evaluation_id']}-stream-smoke.jsonl")
    if not smoke_path.exists():
        await capture_smoke(store, config, smoke_path)
    previous = any(
        run.config.get("evaluation_id") == state["evaluation_id"]
        for run in store.all_runs()
    )
    return await evaluate(
        store,
        config,
        evaluation_id=state["evaluation_id"],
        resume=previous,
        environment=receipt["environment"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["plan", "serve", "run", "backup", "verify", "sample-labels"]
    )
    parser.add_argument("--config", default="eval.yaml")
    parser.add_argument("--db", default="results/live.db")
    parser.add_argument(
        "--state", type=Path, default=Path(".agentreplay-live/session.json")
    )
    parser.add_argument(
        "--receipt", type=Path, default=Path(".agentreplay-live/server.json")
    )
    parser.add_argument("--out", type=Path, default=Path("results/backup"))
    parser.add_argument("--evaluation-id")
    args = parser.parse_args(argv)
    store = None
    try:
        if args.action == "verify":
            result = verify_backup(args.out)
        elif args.action in {"plan", "serve", "run"}:
            config = load_config(args.config)
            command = server_command(config)
            if args.action == "plan":
                result = {
                    "command": command,
                    "tasks": len(config.task_ids)
                    if config.task_ids is not None
                    else len(load_tasks()),
                    "trials": config.trials,
                    "versions": config.versions,
                    "gpu_started": False,
                }
            elif args.action == "serve":
                return serve(config, args.receipt)
            else:
                receipt = validate_receipt(config, args.receipt)
                database = Path(args.db).resolve()
                database.parent.mkdir(parents=True, exist_ok=True)
                lock_path = database.with_suffix(".lock")
                with lock_path.open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    store = TraceStore(database)
                    result = asyncio.run(
                        live_run(
                            store,
                            config,
                            args.state,
                            receipt,
                            args.state.parent / "stream-smoke.jsonl",
                        )
                    )
        else:
            store = TraceStore(args.db)
            id = (
                args.evaluation_id
                or json.loads(args.state.read_text())["evaluation_id"]
            )
            result = (
                preserve(store, args.out, id)
                if args.action == "backup"
                else sample_labels(store, id)
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (
        ValueError,
        TypeError,
        OSError,
        KeyError,
        httpx.HTTPError,
        subprocess.SubprocessError,
        sqlite3.Error,
    ) as error:
        parser.error(str(error))
    finally:
        if store is not None:
            store.close()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
