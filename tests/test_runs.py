import pytest

from agentreplay.runs import RunRecorder
from agentreplay.schema import Run, Trace
from agentreplay.store import TraceStore


def test_run_records_all_config_and_finishes_success(store: TraceStore) -> None:
    trace = store.create_trace(Trace(name="run"))
    run = Run(
        kind="live",
        trace_id=trace.id,
        task_id="task",
        trial=2,
        agent_name="minimal",
        agent_version="v1",
        model="mock",
        config={"z": 0.0, "a": "é"},
        fault_plan_id="plan",
    )
    ticks = iter([10.0, 11.5])
    recorder = RunRecorder(store, run, clock=lambda: next(ticks))
    assert store.get_run(run.id) is None
    with recorder as active:
        assert store.get_run(active.id) == active
        active.task_success = True
        active.detail = "verified"
    assert active.started_at == 10.0 and active.finished_at == 11.5
    assert active.outcome == "completed" and active.detail == "verified"
    assert active.task_success is True
    assert store.get_run(active.id) == active and store.runs(trace.id) == [active]
    assert run.started_at != 10.0 and run.outcome is None
    assert (
        store.conn.execute("SELECT config FROM runs").fetchone()[0]
        == '{"a":"é","z":0.0}'
    )
    assert store.get_run("absent") is None
    assert store.runs("absent") == []


@pytest.mark.parametrize(
    "error,outcome",
    [
        (ValueError("agent failed"), "agent_error"),
        (KeyboardInterrupt("stopped"), "aborted"),
        (SystemExit(7), "aborted"),
    ],
)
def test_run_records_failures_without_suppressing_them(
    store: TraceStore,
    error: BaseException,
    outcome: str,
) -> None:
    trace = store.create_trace(Trace(name="error"))
    ticks = iter([5.0, 6.0])
    recorder = RunRecorder(
        store,
        Run(kind="replay", trace_id=trace.id, replay_mode="strict"),
        clock=lambda: next(ticks),
    )
    with pytest.raises(type(error)) as raised, recorder as run:
        raise error
    assert raised.value is error
    saved = store.get_run(run.id)
    assert saved.outcome == outcome and run.outcome == outcome
    assert saved.detail == f"{type(error).__name__}: {error}"
    assert saved.finished_at == 6.0 and saved.task_success is None


def test_verifier_failure_is_still_completed_and_recorder_cannot_reenter(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="false"))
    recorder = RunRecorder(
        store, Run(kind="record", trace_id=trace.id), clock=lambda: 1.0
    )
    with recorder as run:
        run.task_success = False
    assert store.get_run(run.id).outcome == "completed"
    assert store.get_run(run.id).task_success is False
    with pytest.raises(RuntimeError, match="reused"):
        recorder.__enter__()
    with pytest.raises(ValueError, match="already finished"):
        store.finish_run(run.id, "completed", finished_at=2.0)
    with pytest.raises(ValueError, match="unknown"):
        store.finish_run("absent", "completed", finished_at=2.0)


def test_context_rejects_finished_run(store: TraceStore) -> None:
    trace = store.create_trace(Trace(name="finished"))
    with pytest.raises(ValueError, match="unfinished"), RunRecorder(
        store, Run(kind="record", trace_id=trace.id, outcome="completed")
    ):
        pytest.fail("entered finished context")
    assert store.runs(trace.id) == []
