import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from agentreplay.canonical import canonical_json, tool_args_hash
from agentreplay.replay import ToolReplayer
from agentreplay.schema import ToolEvent, Trace
from agentreplay.store import TraceStore
from agentreplay.tools import ToolRecorder, ToolReplayedError, ToolReplayMiss


def test_record_replay_never_calls_function_and_normalizes_arg_order(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="tools"))
    calls = 0

    def add(a: int, b: int) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"answer": a + b, "nested": [None, True, "é"]}

    ticks = iter([2.0, 2.25])
    record = ToolRecorder(
        store, trace.id, "record", clock=lambda: next(ticks), wall_clock=lambda: 50.0
    )
    result = record.call("add", {"b": 3, "a": 2}, add)
    event = store.tool_events(trace.id)[0]
    assert calls == 1 and result == {"answer": 5, "nested": [None, True, "é"]}
    assert event.args == {"a": 2, "b": 3} and event.result == result
    assert event.args_hash == tool_args_hash({"a": 2, "b": 3})
    assert event.seq == 0 and event.latency_ms == 250.0 and event.created_at == 50.0
    replay = ToolRecorder(store, trace.id, "replay")
    assert replay.call("add", {"a": 2, "b": 3}, add) == result
    assert calls == 1
    assert len(store.tool_events(trace.id)) == 1


def test_repeated_calls_are_consumed_once_in_recorded_order(store: TraceStore) -> None:
    trace = store.create_trace(Trace(name="repeat"))
    record = ToolRecorder(store, trace.id, "record")
    for value in ["first", "second", "third"]:
        record.call("read", {"key": "x"}, lambda key, value=value: value)
    replay = ToolRecorder(store, trace.id, "replay")
    assert [
        replay.call("read", {"key": "x"}, lambda key: pytest.fail("executed"))
        for _ in range(3)
    ] == ["first", "second", "third"]
    with pytest.raises(ToolReplayMiss) as error:
        replay.call("read", {"key": "x"}, lambda key: pytest.fail("executed"))
    assert error.value.name == "read" and error.value.remaining == 0


@pytest.mark.parametrize(
    "exception", [ValueError("bad value"), TimeoutError("late"), KeyError("absent")]
)
def test_exception_is_stored_and_original_reraised_then_replayed(
    store: TraceStore,
    exception: Exception,
) -> None:
    trace = store.create_trace(Trace(name="error"))

    def fail() -> None:
        raise exception

    with pytest.raises(type(exception)) as original:
        ToolRecorder(store, trace.id, "record").call("fail", {}, fail)
    assert original.value is exception
    event = store.tool_events(trace.id)[0]
    assert (
        event.result is None
        and event.error == f"{type(exception).__name__}: {exception}"
    )
    replay = ToolRecorder(store, trace.id, "replay")
    with pytest.raises(ToolReplayedError) as repeated:
        replay.call("fail", {}, lambda: pytest.fail("executed"))
    assert str(repeated.value) == event.error
    assert repeated.value.original_error == event.error
    with pytest.raises(ToolReplayMiss):
        replay.call("fail", {}, fail)


@pytest.mark.parametrize("value", [{1, 2}, object(), float("nan"), float("inf")])
def test_non_json_result_raises_named_type_error(store: TraceStore, value: Any) -> None:
    trace = store.create_trace(Trace(name="bad-json"))
    with pytest.raises(TypeError, match="broken_tool"):
        ToolRecorder(store, trace.id, "record").call("broken_tool", {}, lambda: value)
    event = store.tool_events(trace.id)[0]
    assert event.error.startswith("TypeError:") and "broken_tool" in event.error
    assert event.result is None


def test_null_result_roundtrips_as_success(store: TraceStore) -> None:
    trace = store.create_trace(Trace(name="null"))
    assert (
        ToolRecorder(store, trace.id, "record").call("noop", {}, lambda: None) is None
    )
    assert store.tool_events(trace.id)[0].error is None
    assert (
        ToolRecorder(store, trace.id, "replay").call(
            "noop", {}, lambda: pytest.fail("executed")
        )
        is None
    )


def test_misses_match_both_name_and_arguments_and_do_not_consume(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="miss"))
    ToolRecorder(store, trace.id, "record").call(
        "echo", {"value": 1}, lambda value: value
    )
    replay = ToolRecorder(store, trace.id, "replay")
    for name, args in [("other", {"value": 1}), ("echo", {"value": 2})]:
        with pytest.raises(ToolReplayMiss) as error:
            replay.call(name, args, lambda **kwargs: pytest.fail("executed"))
        assert error.value.remaining == 1 and error.value.args_hash == tool_args_hash(
            args
        )
    assert replay.call("echo", {"value": 1}, lambda value: pytest.fail("executed")) == 1


def test_decorator_handles_positional_keyword_and_default_arguments(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="decorated"))
    record = ToolRecorder(store, trace.id, "record")

    @record.tool("sum")
    def add(a: int, b: int = 3) -> int:
        """Add two values."""
        return a + b

    assert add(2) == 5 and add(b=4, a=2) == 6
    assert add.__name__ == "add" and add.__doc__ == "Add two values."
    replay = ToolRecorder(store, trace.id, "replay")

    @replay.tool("sum")
    def replaced(a: int, b: int = 3) -> int:
        pytest.fail("decorated function executed during replay")

    assert replaced(a=2) == 5 and replaced(2, 4) == 6
    with pytest.raises(TypeError):
        replaced()


def test_replay_instances_and_traces_are_isolated(store: TraceStore) -> None:
    traces = [store.create_trace(Trace(name=name)) for name in ["first", "second"]]
    for index, trace in enumerate(traces):
        ToolRecorder(store, trace.id, "record").call(
            "read", {}, lambda index=index: index
        )
    first = ToolRecorder(store, traces[0].id, "replay")
    same_trace = ToolRecorder(store, traces[0].id, "replay")
    second = ToolRecorder(store, traces[1].id, "replay")
    assert (
        first.call("read", {}, lambda: 99)
        == same_trace.call("read", {}, lambda: 99)
        == 0
    )
    assert second.call("read", {}, lambda: 99) == 1


def test_args_and_results_are_snapshots(store: TraceStore) -> None:
    trace = store.create_trace(Trace(name="snapshots"))
    args = {"values": [1]}
    output = {"items": [2]}

    def mutate(values: list[int]) -> dict[str, list[int]]:
        values.append(3)
        return output

    ToolRecorder(store, trace.id, "record").call("mutate", args, mutate)
    output["items"].append(4)
    event = store.tool_events(trace.id)[0]
    assert event.args == {"values": [1]} and event.result == {"items": [2]}
    assert args == {"values": [1, 3]}


def test_success_inside_exception_handler_is_not_marked_error(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="handled"))
    try:
        raise ValueError("unrelated")
    except ValueError:
        assert ToolRecorder(store, trace.id, "record").call("ok", {}, lambda: 5) == 5
    assert store.tool_events(trace.id)[0].error is None


def test_tool_storage_reopens_and_uses_canonical_json(tmp_path: Path) -> None:
    path = tmp_path / "tools.db"
    store = TraceStore(path)
    trace = store.create_trace(Trace(name="persist"))
    event = ToolEvent(
        trace_id=trace.id,
        seq=0,
        name="read",
        args={"z": 0.0, "a": "é"},
        args_hash=tool_args_hash({"z": 0.0, "a": "é"}),
        result={"z": 1, "a": 2},
    )
    store.add_tool_event(event)
    raw = store.conn.execute("SELECT args,result FROM tool_events").fetchone()
    assert raw["args"] == '{"a":"é","z":0.0}' and raw["result"] == '{"a":2,"z":1}'
    with pytest.raises(sqlite3.IntegrityError):
        store.add_tool_event(event.model_copy(update={"id": "duplicate"}))
    with pytest.raises(sqlite3.IntegrityError):
        store.add_tool_event(
            event.model_copy(update={"id": "orphan", "trace_id": "absent"})
        )
    assert store.next_tool_seq(trace.id) == 1
    assert store.next_seq(trace.id) == 0
    store.close()
    reopened = TraceStore(path)
    assert reopened.tool_events(trace.id) == [event]
    reopened.close()


def test_tool_sequence_reservations_are_thread_safe_and_independent(
    store: TraceStore,
) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        numbers = list(pool.map(lambda _: store.next_tool_seq("t"), range(100)))
    assert sorted(numbers) == list(range(100))
    assert store.next_tool_seq("other") == 0 and store.next_seq("t") == 0


def test_tool_hash_dicts_raw_strings_and_numeric_types() -> None:
    args = {"nested": {"b": 2, "a": "é"}, "value": 0.0}
    assert tool_args_hash(args) == tool_args_hash(json.dumps(args))
    assert (
        tool_args_hash(args)
        == hashlib.sha256(canonical_json(args).encode()).hexdigest()
    )
    raw = '{"broken":'
    assert tool_args_hash(raw) == hashlib.sha256(("raw:" + raw).encode()).hexdigest()
    assert tool_args_hash({"n": 0}) != tool_args_hash({"n": 0.0})
    assert tool_args_hash("[]") == hashlib.sha256(b"raw:[]").hexdigest()


def test_tool_replayer_uses_sequence_order_for_unsorted_input() -> None:
    events = [
        ToolEvent(
            trace_id="t",
            seq=index,
            name="read",
            args={},
            args_hash=tool_args_hash({}),
            result=index,
        )
        for index in [2, 0, 1]
    ]
    replay = ToolReplayer(events)
    assert [replay.next("read", {}).result for _ in range(3)] == [0, 1, 2]


def test_invalid_configuration_and_arguments_fail_before_execution(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="invalid"))
    with pytest.raises(ValueError, match="mode"):
        ToolRecorder(store, trace.id, "invalid")
    with pytest.raises(ValueError, match="unknown trace"):
        ToolRecorder(store, "absent", "record")
    recorder = ToolRecorder(store, trace.id, "record")
    for args in [[], {"value": {1}}]:
        with pytest.raises(TypeError, match="bad"):
            recorder.call("bad", args, lambda **kwargs: pytest.fail("executed"))
    assert store.tool_events(trace.id) == []
    with pytest.raises(TypeError, match="named"):
        recorder.tool("variadic")(lambda *args: None)


@pytest.mark.parametrize("mode", ["record", "replay"])
def test_result_hook_runs_after_base_result_is_available(
    store: TraceStore, mode: str
) -> None:
    trace = store.create_trace(Trace(name="hook"))
    if mode == "replay":
        ToolRecorder(store, trace.id, "record").call("read", {}, lambda: 3)

    def hook(event: ToolEvent, result: Any) -> Any:
        assert store.tool_events(trace.id)[0].id == event.id
        assert result == 3
        return 99

    recorder = ToolRecorder(store, trace.id, mode, faults=hook)
    assert (
        recorder.call(
            "read", {}, lambda: 3 if mode == "record" else pytest.fail("executed")
        )
        == 99
    )
    assert store.tool_events(trace.id)[0].result == 3
