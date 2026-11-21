import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from conftest import chat_req, chat_resp

from agentreplay.checks import check_golden, load_agent
from agentreplay.cli import main
from agentreplay.exportio import export_trace
from agentreplay.schema import Exchange, Trace
from agentreplay.store import TraceStore
from agentreplay.tools import ToolRecorder

GOLDEN = Path(__file__).parent / "golden"


async def changed_request(client: httpx.AsyncClient, recorder: ToolRecorder) -> None:
    response = await client.post(
        "/v1/chat/completions", json=chat_req("changed prompt")
    )
    response.raise_for_status()


async def changed_tool(client: httpx.AsyncClient, recorder: ToolRecorder) -> None:
    recorder.call("unknown", {}, lambda: pytest.fail("executed tool"))


async def short_agent(client: httpx.AsyncClient, recorder: ToolRecorder) -> None:
    return


async def reordered_agent(client: httpx.AsyncClient, recorder: ToolRecorder) -> None:
    for index in [1, 0]:
        response = await client.post("/v1/chat/completions", json=chat_req(str(index)))
        response.raise_for_status()


@pytest.mark.parametrize("mode", ["strict", "lenient"])
def test_unchanged_agent_passes_all_goldens_and_cli(
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    main(
        [
            "check",
            "--golden",
            str(GOLDEN),
            "--agent",
            "examples.minimal_agent:run_agent",
            "--mode",
            mode,
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True and len(report["traces"]) == 2
    assert all(
        trace["outcome"] == "completed" and trace["findings"] == []
        for trace in report["traces"]
    )


@pytest.mark.parametrize(
    "agent,expected",
    [
        ("changed_request", "replay_miss"),
        ("changed_tool", "tool_replay_miss"),
        ("short_agent", "replay_leftover"),
    ],
)
def test_modified_agent_cli_exits_one_with_divergences(
    capsys: pytest.CaptureFixture[str],
    agent: str,
    expected: str,
) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["check", "--golden", str(GOLDEN), "--agent", f"test_checks:{agent}"])
    assert exited.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert all(
        expected in {finding["kind"] for finding in trace["findings"]}
        for trace in report["traces"]
    )


def test_order_warning_fails_strict_check_but_lenient_passes(tmp_path: Path) -> None:
    store = TraceStore()
    trace = store.create_trace(Trace(name="order"))
    for index in range(2):
        store.add_exchange(
            Exchange.from_request(
                trace.id,
                index,
                chat_req(str(index)),
                status_code=200,
                response=chat_resp(),
            )
        )
    export_trace(store, trace.id, tmp_path / "order.jsonl")
    store.close()
    strict = check_golden(tmp_path, "test_checks:reordered_agent")
    lenient = check_golden(tmp_path, "test_checks:reordered_agent", "lenient")
    assert (
        strict["passed"] is False
        and strict["traces"][0]["findings"][0]["kind"] == "replay_order"
    )
    assert lenient["passed"] is True and lenient["traces"][0]["findings"] == []


@pytest.mark.parametrize(
    "reference",
    ["missing-colon", "no_such_module_xyz:agent", "examples.minimal_agent:nope"],
)
def test_invalid_agent_reference_is_configuration_error(reference: str) -> None:
    with pytest.raises(ValueError):
        load_agent(reference)


def test_callable_validation_and_unexpected_errors_finalize(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = ModuleType("custom_check_agents")
    module.not_callable = 5
    module.bad_signature = lambda: None

    class CustomFailure(Exception):
        pass

    def fail(client: Any, recorder: Any) -> None:
        raise CustomFailure("custom failure")

    module.fail = fail
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(TypeError, match="not callable"):
        load_agent("custom_check_agents:not_callable")
    with pytest.raises(ValueError, match="accept"):
        load_agent("custom_check_agents:bad_signature")
    database = tmp_path / "failed-check.db"
    monkeypatch.setattr("agentreplay.checks.TraceStore", lambda: TraceStore(database))
    with pytest.raises(CustomFailure, match="custom failure"):
        check_golden(GOLDEN, "custom_check_agents:fail")
    reopened = TraceStore(database)
    trace = reopened.list_traces()[0]
    run = reopened.runs(trace.id)[0]
    assert (
        run.outcome == "agent_error" and run.detail == "CustomFailure: custom failure"
    )
    assert reopened.findings(run.id)[0].kind == "replay_leftover"
    reopened.close()


def test_repository_agent_import_scope_is_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    golden = GOLDEN.resolve()
    (tmp_path / "local_agent_m4.py").write_text(
        "def agent(client, recorder):\n    return None\n"
    )
    monkeypatch.chdir(tmp_path)
    before = sys.path[:]
    result = check_golden(golden, "local_agent_m4:agent")
    assert result["passed"] is False and sys.path == before


@pytest.mark.parametrize("case", ["empty", "bad_mode", "bad_agent"])
def test_check_config_errors_have_exit_two(
    tmp_path: Path,
    case: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = [
        "check",
        "--golden",
        str(tmp_path if case == "empty" else GOLDEN),
        "--agent",
        "missing" if case == "bad_agent" else "examples.minimal_agent:run_agent",
    ]
    if case == "bad_mode":
        args.extend(["--mode", "wrong"])
    with pytest.raises(SystemExit) as exited:
        main(args)
    assert exited.value.code == 2 and "error:" in capsys.readouterr().err


def test_check_library_rejects_bad_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        check_golden(GOLDEN, "examples.minimal_agent:run_agent", "unknown")


def test_diff_cli_includes_source_request_changes_and_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agentreplay.inspection import render_run_diff
    from agentreplay.schema import Finding, Run

    database = tmp_path / "diff.db"
    store = TraceStore(database)
    runs = []
    for value in ["before", "after"]:
        trace = store.create_trace(Trace(name=value))
        store.add_exchange(
            Exchange.from_request(trace.id, 0, chat_req(value), status_code=200)
        )
        runs.append(store.create_run(Run(kind="replay", trace_id=trace.id)))
    store.add_finding(
        Finding(
            run_id=runs[1].id,
            kind="replay_miss",
            severity="error",
            evidence={"diff": {"changed": [{"path": "/model"}]}},
        )
    )
    with pytest.raises(ValueError, match="exist"):
        render_run_diff(store, runs[0].id, "unknown")
    store.close()
    main(["diff", "--db", str(database), "--run-a", runs[0].id, "--run-b", runs[1].id])
    report = json.loads(capsys.readouterr().out)
    assert (
        report["source_request_diffs"][0]["diff"]["changed"][0]["path"]
        == "/messages/0/content"
    )
    assert (
        report["findings_a"] == [] and report["findings_b"][0]["kind"] == "replay_miss"
    )
