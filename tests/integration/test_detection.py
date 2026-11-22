import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_faults import make_plan

from agentreplay.canonical import canonical_json
from agentreplay.checks import check_golden
from agentreplay.cli import main
from agentreplay.detection import detect_run, load_run_view, make_run_view
from agentreplay.detectors import detect
from agentreplay.evals.faults import evaluate_faults
from agentreplay.exportio import import_trace
from agentreplay.faults import FaultPlan
from agentreplay.proxy import create_app
from agentreplay.runs import RunRecorder
from agentreplay.schema import (
    DetectorConfig,
    Exchange,
    Finding,
    Run,
    RunObservations,
    Trace,
)
from agentreplay.store import TraceStore
from agentreplay.tools import ToolRecorder

GOLDEN = Path(__file__).parents[1] / "golden"


@pytest.mark.parametrize(
    "kind,target,expected",
    [
        ("llm_timeout", "exchange", "timeout"),
        ("llm_http_error", "exchange", "llm_error"),
        ("llm_malformed_json", "exchange", "malformed_response"),
        ("llm_empty_response", "exchange", "malformed_response"),
        ("tool_args_corrupt", "exchange", "invalid_tool_call"),
        ("tool_wrong_name", "exchange", "invalid_tool_call"),
        ("tool_args_schema_violation", "exchange", "invalid_tool_call"),
        ("tool_loop", "exchange", "loop"),
        ("tool_loop", "tool_event", "loop"),
        ("tool_exec_timeout", "tool_event", "tool_error"),
        ("tool_exec_error", "tool_event", "tool_error"),
    ],
)
def test_faulted_observations_detect_after_restart_without_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    target: str,
    expected: str,
) -> None:
    path = tmp_path / "observations.db"
    store = TraceStore(path)
    trace = import_trace(store, GOLDEN / "minimal-add.jsonl")
    before = canonical_json(
        [exchange.model_dump() for exchange in store.exchanges(trace.id)]
    )
    report = evaluate_faults(store, make_plan(kind, target=target), clock=lambda: 123.0)
    run_id = report["runs"][0]["run_id"]
    assert report["scored"] is False
    original = [
        finding for finding in store.findings(run_id) if finding.kind == expected
    ]
    assert len(original) == 1
    assert (
        original[0].exchange_seq if target == "exchange" else original[0].tool_seq
    ) == 0
    snapshot = store.get_observations(run_id)
    assert (
        snapshot is not None
        and len(snapshot.exchanges) == 2
        and len(snapshot.tool_events) == 1
    )
    store.close()
    reopened = TraceStore(path)

    def labels_forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("detector service read the fault labels")

    monkeypatch.setattr(reopened, "faults", labels_forbidden)
    findings = detect_run(reopened, run_id)
    assert [finding for finding in findings if finding.kind == expected] == original
    assert (
        canonical_json(
            [exchange.model_dump() for exchange in reopened.exchanges(trace.id)]
        )
        == before
    )
    view = load_run_view(reopened, run_id)
    assert set(view.run.model_dump()) == {"id", "trace_id", "kind", "outcome"}
    assert "fault_plan_id" not in view.run.model_dump()
    reopened.close()


@pytest.mark.parametrize(
    "filename", ["minimal-add.jsonl", "minimal-add-repeated.jsonl"]
)
def test_clean_golden_has_no_errors_in_runtime_or_reloaded_view(
    store: TraceStore,
    filename: str,
) -> None:
    trace = import_trace(store, GOLDEN / filename)
    clean = FaultPlan.from_dict({"version": 1, "seed": 0, "faults": []})
    result = evaluate_faults(store, clean)["runs"][0]
    assert result["findings"] == []
    observed = load_run_view(store, result["run_id"])
    assert detect(observed) == [] and detect_run(store, result["run_id"]) == []
    assert len(observed.tool_events) == len(store.tool_events(trace.id))
    actual_agent_report = check_golden(GOLDEN, "examples.minimal_agent:run_agent")
    assert actual_agent_report["passed"] is True
    assert all(trace["findings"] == [] for trace in actual_agent_report["traces"])


def test_detect_cli_is_idempotent_and_preserves_replay_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "repeat.db"
    store = TraceStore(database)
    trace = store.create_trace(Trace(name="HTTP error"))
    exchange = Exchange.from_request(
        trace.id, 0, {}, status_code=500, response={"error": {"message": "failed"}}
    )
    store.add_exchange(exchange)
    run = store.create_run(Run(kind="replay", trace_id=trace.id, started_at=10.0))
    existing = Finding(
        run_id=run.id, kind="replay_order", severity="warning", exchange_seq=0
    )
    store.add_finding(existing)
    store.finish_run(
        run.id,
        "completed",
        finished_at=20.0,
        observations=RunObservations(exchanges=[exchange]),
    )
    store.close()
    outputs = []
    for _ in range(2):
        main(["detect", "--db", str(database), "--run-id", run.id])
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1]
    assert [finding["kind"] for finding in json.loads(outputs[0])] == ["llm_error"]
    reopened = TraceStore(database)
    findings = reopened.findings(run.id)
    assert len(findings) == 2 and existing in findings
    assert {finding.kind for finding in findings} == {"llm_error", "replay_order"}
    reopened.close()


def test_replay_budget_counts_misses_and_empty_snapshot_does_not_fall_back(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="empty source"))
    with TestClient(
        create_app(
            store,
            "replay",
            trace_id=trace.id,
            run_config={"detectors": {"max_steps": 2}},
        )
    ) as client:
        run_id = client.app.state.run_id
        for index in range(3):
            assert (
                client.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": str(index)}]},
                ).status_code
                == 409
            )
        finished = client.post(f"/_agentreplay/runs/{run_id}/finish", json={})
        assert finished.status_code == 200
    snapshot = store.get_observations(run_id)
    assert snapshot.exchanges == [] and snapshot.exchange_attempts == 3
    findings = store.findings(run_id)
    assert len([finding for finding in findings if finding.kind == "replay_miss"]) == 3
    assert detect_run(store, run_id)[0].kind == "budget_exceeded"
    assert len(store.findings(run_id)) == 4


def test_snapshot_finalization_and_findings_roll_back_together(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="atomic"))
    run = store.create_run(Run(kind="record", trace_id=trace.id))
    invalid = Finding(run_id="other", kind="timeout", severity="error")
    with pytest.raises(ValueError, match="belong"):
        store.finish_run(
            run.id,
            "completed",
            finished_at=20.0,
            observations=RunObservations(),
            findings=[invalid],
        )
    assert store.get_run(run.id).finished_at is None
    assert store.get_observations(run.id) is None and store.findings(run.id) == []


@pytest.mark.parametrize(
    "problem", ["unknown", "unfinished", "missing_snapshot", "bad_threshold"]
)
def test_detection_cli_configuration_errors_are_clear(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    problem: str,
) -> None:
    database = tmp_path / "invalid.db"
    store = TraceStore(database)
    trace = store.create_trace(Trace(name="config"))
    config = {"detectors": {"max_steps": -1}} if problem == "bad_threshold" else {}
    run = store.create_run(
        Run(
            kind="replay" if problem == "missing_snapshot" else "record",
            trace_id=trace.id,
            config=config,
        )
    )
    if problem in {"missing_snapshot", "bad_threshold"}:
        store.finish_run(run.id, "completed", finished_at=20.0)
    store.close()
    with pytest.raises(SystemExit) as error:
        main(
            [
                "detect",
                "--db",
                str(database),
                "--run-id",
                "absent" if problem == "unknown" else run.id,
            ]
        )
    assert error.value.code == 2 and "error:" in capsys.readouterr().err


def test_record_context_detects_actual_tool_failure_without_injection(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="natural tool failure"))

    def fail() -> None:
        raise ValueError("real tool failed")

    with RunRecorder(
        store, Run(kind="record", trace_id=trace.id), clock=lambda: 20.0
    ) as run:
        with pytest.raises(ValueError, match="real tool failed"):
            ToolRecorder(store, trace.id, "record", clock=lambda: 0.0).call(
                "read", {}, fail
            )
        run.task_success = False
    assert run.outcome == "completed"
    assert [finding.kind for finding in store.findings(run.id)] == ["tool_error"]
    assert (
        store.get_observations(run.id).tool_events[0].error
        == "ValueError: real tool failed"
    )
    assert detect_run(store, run.id) == store.findings(run.id)


def test_bad_result_needs_an_independent_verifier_not_the_fault_schema(
    store: TraceStore,
) -> None:
    import_trace(store, GOLDEN / "minimal-add.jsonl")
    plan = make_plan(
        "tool_bad_result",
        target="tool_event",
        params={"schema": {"type": "integer"}, "value": "bad"},
    )
    result = evaluate_faults(store, plan)["runs"][0]
    assert result["tools"][0]["result"] == "bad"
    assert result["faults"][0]["fault_class"] == "tool_bad_result"
    assert result["findings"] == []


def test_cross_trace_views_and_bad_control_thresholds_fail_before_persistence(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="expected"))
    run = store.create_run(Run(kind="record", trace_id=trace.id))
    wrong = Exchange.from_request("other", 0, {}, status_code=200)
    with pytest.raises(ValueError, match="belong"):
        make_run_view(run, RunObservations(exchanges=[wrong]))
    before = len(store.list_traces())
    with pytest.raises(ValueError):
        create_app(store, "record", run_config={"detectors": {"timeout_ms": -1}})
    assert len(store.list_traces()) == before
    with TestClient(create_app(store, "replay", trace_id=trace.id)) as client:
        count = len(store.runs(trace.id))
        reply = client.post(
            "/_agentreplay/runs", json={"config": {"detectors": {"loop_window": 1}}}
        )
        assert reply.status_code == 400 and len(store.runs(trace.id)) == count


def test_record_run_without_snapshot_can_analyze_captured_rows(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="direct record"))
    store.add_exchange(Exchange.from_request(trace.id, 0, {}, status_code=504))
    run = store.create_run(Run(kind="record", trace_id=trace.id))
    store.finish_run(run.id, "completed", finished_at=20.0)
    assert detect_run(store, run.id)[0].kind == "timeout"
    assert (
        detect_run(store, run.id, DetectorConfig(timeout_ms=0.0))[0].kind == "timeout"
    )
