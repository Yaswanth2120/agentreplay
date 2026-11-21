import pytest
from conftest import chat_req, chat_resp

from agentreplay.canonical import tool_args_hash
from agentreplay.replay import Replayer, ReplayMiss, ReplaySession, json_diff
from agentreplay.runs import RunRecorder
from agentreplay.schema import Exchange, Finding, Run, ToolEvent, Trace
from agentreplay.store import TraceStore
from agentreplay.tools import ToolRecorder, ToolReplayMiss


def fixture_session(store: TraceStore, mode: str = "strict") -> ReplaySession:
    trace = store.create_trace(Trace(name="divergence"))
    for index in range(2):
        store.add_exchange(
            Exchange.from_request(
                trace.id,
                index,
                chat_req(str(index)),
                status_code=200,
                response=chat_resp(str(index)),
            )
        )
    store.add_tool_event(
        ToolEvent(
            trace_id=trace.id,
            seq=0,
            name="read",
            args={},
            args_hash=tool_args_hash({}),
            result=5,
        )
    )
    run = store.create_run(Run(kind="replay", trace_id=trace.id, replay_mode=mode))
    return ReplaySession(store, run, clock=lambda: 123.0)


def test_json_diff_nested_arrays_keys_types_and_truncation() -> None:
    diff = json_diff(
        {"a": [1, 2], "remove": True, "~/": 0}, {"a": [0.0], "add": "x" * 300, "~/": 1}
    )
    assert {item["path"] for item in diff["changed"]} == {"/a/0", "/~0~1"}
    assert {item["path"] for item in diff["removed"]} == {"/a/1", "/remove"}
    assert diff["added"][0]["path"] == "/add"
    assert len(diff["added"][0]["value"]) == 200
    assert json_diff([1], [1, "new"])["added"][0]["path"] == "/1"
    assert json_diff({"same": 0}, {"same": 0.0})["changed"]
    assert json_diff(None, None) == {"added": [], "removed": [], "changed": []}


def test_miss_is_persisted_with_nearest_exchange_diff_and_hash(
    store: TraceStore,
) -> None:
    session = fixture_session(store)
    with pytest.raises(ReplayMiss) as error:
        session.next_exchange(chat_req("changed", temperature=0.7))
    findings = store.findings(session.run.id)
    assert len(findings) == 1 and findings[0].kind == "replay_miss"
    assert findings[0].exchange_seq == 0 and findings[0].severity == "error"
    evidence = findings[0].evidence
    assert evidence == error.value.evidence
    assert evidence["request_hash"] == error.value.request_hash
    assert evidence["nearest_exchange"]["seq"] == 0
    assert evidence["diff"]["changed"][0]["path"] == "/messages/0/content"
    assert evidence["diff"]["added"][0]["path"] == "/temperature"
    assert session.replayer.remaining == 2


def test_nearest_falls_back_to_message_count_even_after_consumption() -> None:
    exchanges = [
        Exchange.from_request(
            "t",
            index + 10,
            {"messages": [{"role": "user", "content": "x"}] * count},
            status_code=200,
        )
        for index, count in enumerate([5, 2, 9])
    ]
    replay = Replayer(exchanges)
    replay.next(exchanges[1].request)
    with pytest.raises(ReplayMiss) as error:
        replay.next({"messages": [{"role": "user", "content": "different"}] * 2})
    assert error.value.evidence["nearest_exchange"]["seq"] == 11
    with pytest.raises(ReplayMiss) as empty:
        Replayer([]).next(chat_req())
    assert empty.value.evidence["nearest_exchange"] is None
    assert empty.value.evidence["diff"]["added"]


@pytest.mark.parametrize("mode", ["strict", "lenient"])
def test_order_warning_only_in_strict_mode(store: TraceStore, mode: str) -> None:
    session = fixture_session(store, mode)
    assert session.next_exchange(chat_req("1")).seq == 1
    findings = store.findings(session.run.id)
    if mode == "strict":
        assert len(findings) == 1 and findings[0].kind == "replay_order"
        assert findings[0].severity == "warning" and findings[0].exchange_seq == 1
        assert findings[0].evidence == {"expected_seq": 0, "actual_seq": 1}
    else:
        assert findings == []
    session.next_exchange(chat_req("0"))
    assert len(store.findings(session.run.id)) == len(findings)


def test_tool_miss_and_finish_cover_all_remaining_events(store: TraceStore) -> None:
    session = fixture_session(store)
    recorder = ToolRecorder(store, session.run.trace_id, "replay", session=session)
    with pytest.raises(ToolReplayMiss):
        recorder.call("wrong", {}, lambda: pytest.fail("executed"))
    miss = store.findings(session.run.id)[0]
    assert miss.kind == "tool_replay_miss" and miss.tool_seq == 0
    assert miss.evidence["name"] == "wrong" and miss.evidence[
        "args_hash"
    ] == tool_args_hash({})
    session.next_exchange(chat_req("0"))
    session.finish("completed", task_success=False)
    leftover = store.findings(session.run.id)[-1]
    assert leftover.kind == "replay_leftover" and leftover.severity == "error"
    assert leftover.evidence == {"exchange_seqs": [1], "tool_seqs": [0]}
    assert leftover.exchange_seq == 1 and leftover.tool_seq == 0
    assert store.get_run(session.run.id).task_success is False
    with pytest.raises(ValueError, match="finished"):
        session.finish("completed")
    with pytest.raises(ValueError, match="finished"):
        session.next_exchange(chat_req("1"))
    with pytest.raises(ValueError, match="finished"):
        recorder.call("read", {}, lambda: 99)
    assert len(store.findings(session.run.id)) == 2


def test_full_replay_finishes_without_leftover_findings(store: TraceStore) -> None:
    session = fixture_session(store)
    session.next_exchange(chat_req("0"))
    session.next_exchange(chat_req("1"))
    assert session.next_tool("read", {}).result == 5
    assert session.tools.remaining_seqs == []
    assert session.replayer.remaining_seqs == []
    assert session.finish("completed").finished_at == 123.0
    assert store.findings(session.run.id) == []


def test_context_detects_leftovers_on_exception_exit(store: TraceStore) -> None:
    source = fixture_session(store)
    context = RunRecorder(
        store, Run(kind="replay", trace_id=source.run.trace_id), clock=lambda: 321.0
    )
    with pytest.raises(RuntimeError, match="failed"), context as run:
        context.replay.next_exchange(chat_req("0"))
        raise RuntimeError("failed")
    assert run.outcome == "agent_error"
    assert store.findings(run.id)[0].kind == "replay_leftover"


def test_finalization_and_findings_are_one_transaction(store: TraceStore) -> None:
    session = fixture_session(store)
    invalid = Finding(run_id="other", kind="replay_leftover", severity="error")
    with pytest.raises(ValueError, match="belong"):
        store.finish_run(
            session.run.id, "completed", finished_at=5.0, findings=[invalid]
        )
    assert store.get_run(session.run.id).finished_at is None
    assert store.findings(session.run.id) == []


def test_sessions_reject_forged_runs_and_tool_binding(store: TraceStore) -> None:
    session = fixture_session(store)
    other = store.create_trace(Trace(name="other"))
    with pytest.raises(ValueError, match="existing"):
        ReplaySession(store, session.run.model_copy(update={"trace_id": other.id}))
    with pytest.raises(ValueError, match="same"):
        ToolRecorder(store, other.id, "replay", session=session)
    with pytest.raises(ValueError, match="same"):
        ToolRecorder(store, session.run.trace_id, "record", session=session)
    with pytest.raises(ValueError, match="replay run"):
        ReplaySession(store, Run(kind="record", trace_id=other.id))
    with pytest.raises(ValueError, match="mode"):
        Replayer([], "unknown")
