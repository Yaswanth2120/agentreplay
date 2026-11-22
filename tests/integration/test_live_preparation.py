"""M9 preparation and failure recovery without GPUs or network connections."""

from __future__ import annotations

import asyncio
import json
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentreplay.evals import harness, live
from agentreplay.evals.harness import evaluate, load_config
from agentreplay.evals.preservation import file_sha256, preserve, verify_backup
from agentreplay.evals.resume import effective_runs, index_runs, job_key
from agentreplay.evals.scripted import scripted_response
from agentreplay.exportio import import_trace
from agentreplay.report import build_report
from agentreplay.schema import EvaluationConfig, Run, Trace
from agentreplay.store import TraceStore
from agentreplay.streaming import synthesize_chunks


@pytest.fixture
def config() -> EvaluationConfig:
    return EvaluationConfig(
        versions=["v1"],
        task_ids=["kv-017"],
        max_tokens=128,
        chat_template_kwargs={"enable_thinking": False},
        bootstrap_resamples=100,
    )


def test_complete_resume_skips_paid_calls_including_failed_task_results(
    store: TraceStore,
) -> None:
    config = EvaluationConfig(
        versions=["v1", "v2"], task_ids=["kv-017"], bootstrap_resamples=100
    )
    count = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return scripted_response(request)

    async def exercise() -> None:
        result = await evaluate(
            store, config, transport=httpx.MockTransport(upstream), git_sha="stable"
        )
        before, run_count = count, len(store.all_runs())
        again = await evaluate(
            store,
            config,
            evaluation_id=result["evaluation_id"],
            resume=True,
            transport=httpx.MockTransport(upstream),
            git_sha="stable",
        )
        assert (
            again == result and count == before and len(store.all_runs()) == run_count
        )
        report = build_report(store)
        assert report["metrics"]["versions"]["v2"]["task_success"]["successes"] == 0
        assert report["config"]["superseded_attempts"] == []

    asyncio.run(exercise())


def test_cancelled_attempt_is_retried_without_repeating_completed_pairs(
    store: TraceStore, config: EvaluationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = harness.run_agent
    requests = []

    async def interrupted(*args: Any, **kwargs: Any) -> str:
        if kwargs["trial"] == 1:
            raise asyncio.CancelledError
        return await original(*args, **kwargs)

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return scripted_response(request)

    monkeypatch.setattr(harness, "run_agent", interrupted)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            evaluate(
                store, config, transport=httpx.MockTransport(upstream), git_sha="stable"
            )
        )
    prior = store.all_runs()
    assert (
        len(prior) == 2
        and prior[0].task_success is True
        and prior[1].outcome == "aborted"
    )
    id = prior[0].config["evaluation_id"]
    monkeypatch.setattr(harness, "run_agent", original)
    result = asyncio.run(
        evaluate(
            store,
            config,
            evaluation_id=id,
            resume=True,
            transport=httpx.MockTransport(upstream),
            git_sha="stable",
            environment={"gpu": "fixture"},
        )
    )
    assert len(result["run_ids"]) == 9 and len(store.all_runs()) == 10
    assert (
        len(requests) == 6
    )  # exactly 2 requests per task/trial; trial0 was never repeated.
    assert all(
        request["max_tokens"] == 128
        and request["chat_template_kwargs"] == {"enable_thinking": False}
        for request in requests
    )
    report = build_report(store)
    assert report["metrics"]["n_runs"] == 9
    assert report["config"]["superseded_attempts"] == [prior[1].id]
    assert report["config"]["environments"] == [{"gpu": "fixture"}]
    attempts = [
        run
        for run in effective_runs(store.all_runs())
        if run.config["phase"] == "record" and run.trial == 1
    ]
    assert attempts[0].config["attempt"] == 1


def test_hard_interrupted_unfinished_row_is_aborted_then_retried(
    store: TraceStore, config: EvaluationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = harness.run_task

    async def interrupt(*args: Any, **kwargs: Any) -> Run:
        if args[2] == 1:
            raise RuntimeError("simulated hard stop before row creation")
        return await original(*args, **kwargs)

    monkeypatch.setattr(harness, "run_task", interrupt)
    with pytest.raises(RuntimeError):
        asyncio.run(evaluate(store, config, git_sha="stable"))
    first = store.all_runs()[0]
    trace = store.create_trace(Trace(name="interrupted"))
    unfinished = first.model_copy(
        deep=True,
        update={
            "id": "unfinished",
            "trace_id": trace.id,
            "trial": 1,
            "finished_at": None,
            "outcome": None,
            "task_success": None,
        },
    )
    unfinished.config.update(trial=1, sampling={"temperature": 0.7, "seed": 1})
    store.create_run(unfinished)
    monkeypatch.setattr(harness, "run_task", original)
    asyncio.run(
        evaluate(
            store,
            config,
            evaluation_id=first.config["evaluation_id"],
            resume=True,
            git_sha="stable",
        )
    )
    assert store.get_run("unfinished").outcome == "aborted"
    assert build_report(store)["config"]["superseded_attempts"] == ["unfinished"]


def test_resume_refuses_changed_provenance_and_unknown_cohorts(
    store: TraceStore, config: EvaluationConfig
) -> None:
    result = asyncio.run(evaluate(store, config, git_sha="stable"))
    id, count = result["evaluation_id"], len(store.all_runs())
    cases = [
        (config, "changed"),
        (config.model_copy(update={"max_tokens": 256}), "stable"),
        (config.model_copy(update={"model_revision": "a" * 40}), "stable"),
    ]
    for changed, sha in cases:
        with pytest.raises(ValueError, match="changed"):
            asyncio.run(
                evaluate(store, changed, evaluation_id=id, resume=True, git_sha=sha)
            )
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(evaluate(store, config, evaluation_id=id, git_sha="stable"))
    with pytest.raises(ValueError, match="unknown"):
        asyncio.run(evaluate(store, config, evaluation_id="missing", resume=True))
    with pytest.raises(ValueError, match="requires"):
        asyncio.run(evaluate(store, config, resume=True))
    assert len(store.all_runs()) == count
    rows = store.all_runs()
    with pytest.raises(ValueError, match="duplicate"):
        index_runs([rows[0], rows[0]])
    with pytest.raises(ValueError):
        job_key(rows[0].model_copy(update={"config": {}}))


def test_fault_sweep_reuses_live_sources_and_resumes_each_plan(
    store: TraceStore, config: EvaluationConfig
) -> None:
    live_config = load_config("eval.yaml")
    plans = live_config.fault_plans[:2]
    config = config.model_copy(update={"fault_plans": plans})
    count = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return scripted_response(request)

    result = asyncio.run(
        evaluate(
            store, config, transport=httpx.MockTransport(upstream), git_sha="stable"
        )
    )
    assert count == 6 and len(result["run_ids"]) == 15
    faults = [r for r in store.all_runs() if r.config["phase"] == "fault_replay"]
    assert len(faults) == 6 and len({r.fault_plan_id for r in faults}) == 2
    report = build_report(store)
    assert report["metrics"]["injected"]["per_kind"]["timeout"]["tp"] == 3
    assert report["metrics"]["injected"]["per_kind"]["llm_error"]["tp"] == 3
    asyncio.run(
        evaluate(
            store,
            config,
            evaluation_id=result["evaluation_id"],
            resume=True,
            transport=httpx.MockTransport(upstream),
            git_sha="stable",
        )
    )
    assert count == 6 and len(store.all_runs()) == 15
    duplicate = config.model_copy(update={"fault_plans": [plans[0], plans[0]]})
    with pytest.raises(ValueError, match="duplicate"):
        asyncio.run(evaluate(store, duplicate))


def test_consistent_backup_with_open_wal_and_goldens_round_trip(
    tmp_path: Path, config: EvaluationConfig
) -> None:
    path = tmp_path / "source.db"
    store = TraceStore(path)
    try:
        result = asyncio.run(evaluate(store, config, git_sha="stable"))
        assert Path(store.database_path()) == path
        assert path.with_name(path.name + "-wal").exists()
        folder = tmp_path / "preserved"
        output = preserve(store, folder, result["evaluation_id"])
        assert output["report_status"] == "complete"
        assert file_sha256(Path(output["archive"])) == output["sha256"]
        assert verify_backup(folder)["evaluation_id"] == result["evaluation_id"]
        copied = TraceStore(folder / "results.db")
        try:
            assert build_report(copied) == build_report(store)
        finally:
            copied.close()
        goldens = list((folder / "goldens").glob("*.jsonl"))
        assert len(goldens) == 3
        imported = TraceStore()
        try:
            for golden in goldens:
                assert import_trace(imported, golden).task_id == "kv-017"
        finally:
            imported.close()
        with tarfile.open(output["archive"]) as archive:
            assert "preserved/results.db" in archive.getnames()
            assert (
                len([name for name in archive.getnames() if name.endswith(".jsonl")])
                == 5
            )
        with pytest.raises(FileExistsError):
            preserve(store, folder, result["evaluation_id"])
        with pytest.raises(FileExistsError):
            store.backup(path)
        assert len(store.all_runs()) == 9
        (folder / "tasks.jsonl").write_text("tampered")
        with pytest.raises(ValueError, match="checksum"):
            verify_backup(folder)
    finally:
        store.close()


def test_partial_backup_is_disclosed_and_manifest_paths_validated(
    store: TraceStore, tmp_path: Path
) -> None:
    trace = store.create_trace(Trace(name="partial"))
    run = store.create_run(
        Run(
            kind="live",
            trace_id=trace.id,
            config={"evaluation_id": "partial", "phase": "record"},
        )
    )
    output = preserve(store, tmp_path / "partial", "partial")
    assert output["report_status"].startswith("incomplete:")
    folder = Path(output["directory"])
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"]["../escape"] = "fake"
    (folder / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest path"):
        verify_backup(folder)
    assert store.get_run(run.id).finished_at is None
    with pytest.raises(ValueError, match="unknown"):
        preserve(store, tmp_path / "unknown", "absent")


def test_live_plan_is_local_pinned_and_does_not_start_gpu(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert live.main(["plan"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["gpu_started"] is False and output["tasks"] == 200
    config = load_config("eval.yaml")
    command = live.server_command(config)
    assert command[-1] == config.model_revision and len(config.fault_plans) == 12
    assert config.versions == ["v1", "v2"] and config.trials == 3
    assert config.max_tokens == 1024 and config.chat_template_kwargs == {
        "enable_thinking": False
    }
    for changed in [
        config.model_copy(update={"model_revision": "main"}),
        config.model_copy(update={"upstream_url": "http://user:secret@localhost:8000"}),
        config.model_copy(update={"vllm_flags": ["--api-key", "secret"]}),
    ]:
        with pytest.raises(ValueError):
            live.server_command(changed)


def test_real_stream_smoke_capture_against_fake_fragmented_sse(
    store: TraceStore, tmp_path: Path
) -> None:
    config = load_config("eval.yaml")
    message = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call0",
                            "type": "function",
                            "function": {
                                "name": "calculate",
                                "arguments": '{"op":"add","a":2,"b":3}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    chunks = synthesize_chunks(message)
    body = "\n\n".join(chunks) + "\n\n"

    def upstream(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    out = tmp_path / "stream.jsonl"
    trace_id = asyncio.run(
        live.capture_smoke(store, config, out, transport=httpx.MockTransport(upstream))
    )
    assert out.exists() and store.exchanges(trace_id)[0].chunks == chunks
    assert store.tool_events(trace_id)[0].result == 5
    assert store.runs(trace_id)[0].task_success is True

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            live.capture_smoke(
                store,
                config,
                tmp_path / "failed.jsonl",
                transport=httpx.MockTransport(bad),
            )
        )
    assert all(run.finished_at is not None for run in store.all_runs())


def test_server_receipt_and_environment_capture_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    config = load_config("eval.yaml")
    distributions = [
        SimpleNamespace(metadata={"Name": "vllm"}, version=config.vllm_version)
    ]
    monkeypatch.setattr(live.importlib.metadata, "distributions", lambda: distributions)
    monkeypatch.setattr(
        live.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(
            stdout="fixture GPU, driver, 49152 MiB, uuid\n"
            if args[0] == "nvidia-smi"
            else "12.8\n"
        ),
    )
    monkeypatch.setattr(live.platform, "platform", lambda: "Linux fixture")
    monkeypatch.setattr(live, "git_revision", lambda: "fixture-sha")
    environment = live.collect_environment(config)
    assert (
        environment["packages"]["vllm"] == config.vllm_version
        and environment["torch_cuda"] == "12.8"
    )
    distributions[0].version = "wrong"
    with pytest.raises(ValueError, match="install"):
        live.collect_environment(config)
    command = live.server_command(config)
    receipt_path = tmp_path / "server.json"
    live.write_json(
        receipt_path,
        {
            "status": "running",
            "pid": 123,
            "command": command,
            "environment": environment,
        },
    )
    monkeypatch.setattr(live.os, "kill", lambda pid, signal: None)
    original_read = Path.read_bytes

    def cmdline(path: Path) -> bytes:
        return (
            ("\0".join(command) + "\0").encode()
            if str(path) == "/proc/123/cmdline"
            else original_read(path)
        )

    monkeypatch.setattr(Path, "read_bytes", cmdline)
    assert live.validate_receipt(config, receipt_path)["pid"] == 123
    with pytest.raises(ValueError, match="already running"):
        live.serve(config, receipt_path)
    record = json.loads(receipt_path.read_text())
    record["command"] = ["wrong"]
    live.write_json(receipt_path, record)
    with pytest.raises(ValueError, match="receipt"):
        live.validate_receipt(config, receipt_path)


def test_server_process_exit_updates_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        pid = 123
        returncode = 0

        def wait(self, **kwargs: Any) -> int:
            return 0

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(live, "collect_environment", lambda config: {"fixture": True})

    def start(command: list[str], *, start_new_session: bool) -> Process:
        assert start_new_session is True
        return Process()

    monkeypatch.setattr(live.subprocess, "Popen", start)
    monkeypatch.setattr(live.os, "killpg", lambda pid, sig: None)
    receipt = tmp_path / "server.json"
    assert live.serve(load_config("eval.yaml"), receipt) == 0
    assert json.loads(receipt.read_text())["status"] == "exited"


def test_live_session_is_saved_before_work_and_database_identity_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config("eval.yaml")
    store = TraceStore(tmp_path / "session.db")
    state, smoke = tmp_path / "session.json", tmp_path / "smoke.jsonl"
    original_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"id": config.model}]})
        )
        return original_client(*args, **kwargs)

    monkeypatch.setattr(live.httpx, "AsyncClient", client)
    captured = []

    async def smoke_capture(
        store: TraceStore, config: EvaluationConfig, path: Path
    ) -> str:
        path.write_text("fixture")
        return "smoke"

    async def evaluation(
        store: TraceStore, config: EvaluationConfig, **kwargs: Any
    ) -> dict[str, Any]:
        assert json.loads(state.read_text())["evaluation_id"] == kwargs["evaluation_id"]
        captured.append(kwargs)
        return {"evaluation_id": kwargs["evaluation_id"]}

    monkeypatch.setattr(live, "capture_smoke", smoke_capture)
    monkeypatch.setattr(live, "evaluate", evaluation)
    try:
        first = asyncio.run(
            live.live_run(store, config, state, {"environment": {}}, smoke)
        )
        second = asyncio.run(
            live.live_run(store, config, state, {"environment": {}}, smoke)
        )
        assert first == second and captured[0]["resume"] is False
        saved = json.loads(state.read_text())
        saved["database"] = "/different/db"
        live.write_json(state, saved)
        with pytest.raises(ValueError, match="different database"):
            asyncio.run(live.live_run(store, config, state, {"environment": {}}, smoke))
        trace = store.create_trace(Trace(name="restored"))
        store.create_run(
            Run(
                kind="live",
                trace_id=trace.id,
                config={"evaluation_id": first["evaluation_id"]},
            )
        )
        asyncio.run(live.live_run(store, config, state, {"environment": {}}, smoke))
        assert captured[-1]["resume"] is True
        assert json.loads(state.read_text())["database"] == store.database_path()
    finally:
        store.close()


def test_manual_sample_is_stratified_deterministic_and_contains_no_detector_output(
    store: TraceStore,
) -> None:
    id = "sample"
    for env in ["calc", "kv", "files", "search", "tickets"]:
        trace = store.create_trace(Trace(name=env))
        for trial in range(3):
            run = store.create_run(
                Run(
                    kind="live",
                    trace_id=trace.id,
                    task_id=f"{env}-001",
                    trial=trial,
                    agent_version="v1",
                    config={
                        "evaluation_id": id,
                        "phase": "record",
                        "task": {"env": env},
                    },
                )
            )
            from agentreplay.schema import EvaluationResult

            store.finish_run(
                run.id,
                "completed",
                finished_at=1,
                task_success=False,
                evaluation=EvaluationResult(
                    run_id=run.id,
                    final_answer="",
                    final_state={},
                    state_source="executed",
                ),
            )
    sample = live.sample_labels(store, id, per_env=2)
    assert sample == live.sample_labels(store, id, per_env=2) and len(sample) == 10
    assert all(set(row) == {"run_id", "env", "task_id", "trial"} for row in sample)
    with pytest.raises(ValueError, match="not enough"):
        live.sample_labels(store, id)


def test_replay_only_resume_cannot_make_new_model_calls(
    store: TraceStore, config: EvaluationConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = asyncio.run(evaluate(store, config, git_sha="stable"))

    async def forbidden(*args: Any, **kwargs: Any) -> Run:
        pytest.fail("completed replay-only evaluation executed a task again")

    monkeypatch.setattr(harness, "run_task", forbidden)
    assert (
        asyncio.run(
            evaluate(
                store,
                config,
                evaluation_id=result["evaluation_id"],
                resume=True,
                replay_only=True,
                git_sha="stable",
            )
        )
        == result
    )
    with pytest.raises(ValueError, match="existing"):
        asyncio.run(evaluate(store, config, replay_only=True, git_sha="stable"))
    changed = store.all_runs()[0]
    # A higher unfinished attempt must block replay-only before executing any slot.
    unfinished = changed.model_copy(
        deep=True,
        update={
            "id": "unfinished-replay-only",
            "finished_at": None,
            "outcome": None,
            "task_success": None,
        },
    )
    unfinished.config["attempt"] = 1
    store.create_run(unfinished)
    with pytest.raises(ValueError, match="source pairs"):
        asyncio.run(
            evaluate(
                store,
                config,
                evaluation_id=result["evaluation_id"],
                resume=True,
                replay_only=True,
                git_sha="stable",
            )
        )


def test_all_live_fault_variants_execute_offline_without_rerecording_sources(
    store: TraceStore,
) -> None:
    config = load_config("eval.yaml").model_copy(
        update={"task_ids": ["calc-017", "kv-017"], "bootstrap_resamples": 100}
    )
    count = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return scripted_response(request)

    result = asyncio.run(
        evaluate(
            store,
            config,
            transport=httpx.MockTransport(upstream),
            git_sha="mock-hardware-test",
            environment={"backend": "mock"},
        )
    )
    assert count == 36 and len(result["run_ids"]) == 114
    report = build_report(store)
    assert report["metrics"]["injected"]["n_runs"] == 72
    for kind in [
        "timeout",
        "llm_error",
        "malformed_response",
        "invalid_tool_call",
        "loop",
        "tool_error",
    ]:
        assert report["metrics"]["injected"]["per_kind"][kind]["tp"] > 0
    assert report["metrics"]["injected"]["unscored_faults"] == 3


def test_supervisor_signal_cleans_server_session_and_restores_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    previous = {sig: signal.getsignal(sig) for sig in [signal.SIGTERM, signal.SIGHUP]}
    groups = []

    class Process:
        pid = 123
        returncode = None

        def wait(self, **kwargs: Any) -> int:
            if self.returncode is None:
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

    def start(command: list[str], *, start_new_session: bool) -> Process:
        assert start_new_session
        return Process()

    monkeypatch.setattr(live, "collect_environment", lambda config: {"fixture": True})
    monkeypatch.setattr(live.subprocess, "Popen", start)
    monkeypatch.setattr(live.os, "killpg", lambda pid, sig: groups.append((pid, sig)))
    receipt = tmp_path / "server.json"
    with pytest.raises(SystemExit) as stopped:
        live.serve(load_config("eval.yaml"), receipt)
    assert stopped.value.code == 128 + signal.SIGTERM
    assert json.loads(receipt.read_text())["status"] == "exited"
    assert groups == [(123, signal.SIGTERM)]
    assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())


def test_shutdown_terminates_real_child_process_group_without_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal
    import subprocess
    import sys
    import time

    ready, stopped = tmp_path / "ready", tmp_path / "stopped"
    child = """
import signal
import sys
import time
from pathlib import Path

def finish(signum, frame):
    Path(sys.argv[2]).write_text("stopped")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, finish)
Path(sys.argv[1]).write_text("ready")
time.sleep(30)
"""
    parent = """
import subprocess
import sys
import time
subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]])
time.sleep(30)
"""
    command = [sys.executable, "-c", parent, child, str(ready), str(stopped)]
    actual_popen = subprocess.Popen

    class Process:
        def __init__(self) -> None:
            self.child = actual_popen(command, start_new_session=True)
            self.pid = self.child.pid
            self.first_wait = True

        @property
        def returncode(self) -> int | None:
            return self.child.returncode

        def poll(self) -> int | None:
            return self.child.poll()

        def terminate(self) -> None:
            self.child.terminate()

        def wait(self, timeout: float | None = None) -> int:
            if self.first_wait:
                self.first_wait = False
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert ready.exists()
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)
            return self.child.wait(timeout=timeout)

    def start(command: list[str], *, start_new_session: bool) -> Process:
        assert start_new_session
        return Process()

    monkeypatch.setattr(live, "server_command", lambda config: command)
    monkeypatch.setattr(live, "collect_environment", lambda config: {"fixture": True})
    monkeypatch.setattr(live.subprocess, "Popen", start)
    with pytest.raises(SystemExit):
        live.serve(load_config("eval.yaml"), tmp_path / "server.json")
    deadline = time.monotonic() + 5
    while not stopped.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stopped.read_text() == "stopped"
