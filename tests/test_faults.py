import hashlib
import json
import random
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from conftest import chat_req, chat_resp

from agentreplay.canonical import canonical_json, tool_args_hash
from agentreplay.fault_runtime import FaultRuntime
from agentreplay.faults import (
    FaultPlan,
    FaultPlanError,
    FaultSelector,
    invalid_arguments,
    mutate_exchange,
    plan_config,
)
from agentreplay.schema import Exchange, Fault, Run, ToolEvent, Trace
from agentreplay.store import TraceStore
from agentreplay.tools import ToolRecorder


def make_plan(
    kind: str,
    *,
    target: str = "exchange",
    select: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> FaultPlan:
    return FaultPlan.from_dict(
        {
            "version": 1,
            "seed": 17,
            "faults": [
                {
                    "class": kind,
                    "target": target,
                    "select": select or {"seq": [0]},
                    "params": params or {},
                },
            ],
        }
    )


def tool_exchange(trace_id: str = "trace", seq: int = 0) -> Exchange:
    request = chat_req(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "add",
                    "parameters": {
                        "type": "object",
                        "properties": {"a": {"type": "integer"}},
                        "required": ["a"],
                    },
                },
            }
        ]
    )
    return Exchange.from_request(
        trace_id,
        seq,
        request,
        status_code=200,
        response=chat_resp(None, tool=("add", '{"a":1}')),
    )


def runtime_for(
    store: TraceStore, plan: FaultPlan, trace_id: str | None = None
) -> FaultRuntime:
    trace_id = trace_id or store.create_trace(Trace(name="faults")).id
    run = store.create_run(
        Run(
            kind="replay",
            trace_id=trace_id,
            replay_mode="strict",
            fault_plan_id=plan.digest,
            config=plan_config(plan),
        )
    )
    return FaultRuntime(store, run, clock=lambda: 123.0)


def test_yaml_load_hash_and_defaults_are_canonical(tmp_path: Path) -> None:
    path = tmp_path / "plan.yaml"
    path.write_text(
        "version: 1\nseed: 17\nfaults:\n  - class: llm_http_error\n    target: exchange\n    select: {seq: [0]}\n"
    )
    loaded = FaultPlan.load(path)
    explicit = make_plan("llm_http_error", params={"status": 500})
    assert loaded.digest == explicit.digest and len(loaded.digest) == 64
    assert (
        loaded.digest
        == hashlib.sha256(
            canonical_json(loaded.model_dump(by_alias=True)).encode()
        ).hexdigest()
    )
    assert loaded.model_dump(by_alias=True)["faults"][0]["class"] == "llm_http_error"
    assert loaded.faults[0].params == {"status": 500}
    assert plan_config(None) == {}
    assert (
        FaultPlan.from_dict(plan_config(loaded)["fault_plan"]).digest == loaded.digest
    )


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"version": True},
        {"seed": True},
        {"class": "unknown"},
        {"target": "bad"},
        {"target": "tool_event"},
        {"select": {}},
        {"select": {"seq": [0], "probability": 0.1}},
        {"select": {"seq": []}},
        {"select": {"seq": [-1]}},
        {"select": {"seq": [0, 0]}},
        {"select": {"seq": [True]}},
        {"select": {"probability": -0.1}},
        {"select": {"probability": 1.1}},
        {"select": {"probability": float("nan")}},
        {"select": {"every_nth": 0}},
        {"select": {"every_nth": True}},
        {"params": {"status": 200}},
        {"params": {"status": True}},
        {"params": {"unknown": 1}},
    ],
)
def test_invalid_plans_fail_clearly_at_load(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    value = {
        "version": 1,
        "seed": 17,
        "faults": [
            {"class": "llm_http_error", "target": "exchange", "select": {"seq": [0]}},
        ],
    }
    for key, item in change.items():
        if key in {"version", "seed"}:
            value[key] = item
        else:
            value["faults"][0][key] = item
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(value))
    with pytest.raises(FaultPlanError, match="invalid fault plan"):
        FaultPlan.load(path)


@pytest.mark.parametrize("text", ["[broken", "!!python/object:thing {}", "null", "[]"])
def test_bad_yaml_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(text)
    with pytest.raises(FaultPlanError):
        FaultPlan.load(path)
    with pytest.raises(FaultPlanError, match="cannot load"):
        FaultPlan.load(tmp_path / "absent")


@pytest.mark.parametrize(
    "kind,target,params",
    [
        ("tool_loop", "exchange", {"repeat": 2}),
        ("llm_timeout", "exchange", {"delay_ms": 0}),
        ("tool_wrong_name", "exchange", {"name": ""}),
        ("tool_exec_error", "tool_event", {"exception": "UnknownError"}),
        ("tool_bad_result", "tool_event", {}),
        ("tool_bad_result", "tool_event", {"schema": {"type": "integer"}, "value": 1}),
        ("tool_bad_result", "tool_event", {"schema": {"type": "nonsense"}}),
        (
            "tool_bad_result",
            "tool_event",
            {"schema": {"type": "integer"}, "value": {1: "bad"}},
        ),
        (
            "tool_bad_result",
            "tool_event",
            {"schema": {"$ref": "https://invalid.test/schema"}},
        ),
    ],
)
def test_bad_class_parameters_are_rejected(
    kind: str, target: str, params: dict[str, Any]
) -> None:
    with pytest.raises(FaultPlanError):
        make_plan(kind, target=target, params=params)


def test_selector_matches_canonical_random_order_and_is_arrival_independent() -> None:
    plan = FaultPlan.from_dict(
        {
            "version": 1,
            "seed": 7,
            "faults": [
                {
                    "class": "llm_empty_response",
                    "target": "exchange",
                    "select": {"probability": 0.5},
                },
                {
                    "class": "tool_exec_error",
                    "target": "tool_event",
                    "select": {"probability": 0.5},
                },
            ],
        }
    )
    forward, backward = FaultSelector(plan), FaultSelector(plan)
    rng = random.Random(7)
    draws = [[rng.random(), rng.random()] for _ in range(20)]
    actual = {seq: bool(forward.selected("exchange", seq)) for seq in range(20)}
    assert actual == {seq: draws[seq][0] < 0.5 for seq in range(20)}
    assert actual == {
        seq: bool(backward.selected("exchange", seq)) for seq in reversed(range(20))
    }
    assert {seq: bool(backward.selected("tool_event", seq)) for seq in range(20)} == {
        seq: draws[seq][1] < 0.5 for seq in range(20)
    }
    with pytest.raises(ValueError, match="nonnegative"):
        forward.selected("exchange", -1)
    for probability, expected in [(0.0, False), (1.0, True)]:
        selector = FaultSelector(
            make_plan("llm_empty_response", select={"probability": probability})
        )
        assert all(
            bool(selector.selected("exchange", seq)) is expected for seq in range(5)
        )
    nth = FaultSelector(make_plan("llm_empty_response", select={"every_nth": 3}))
    assert [seq for seq in range(10) if nth.selected("exchange", seq)] == [2, 5, 8]


@pytest.mark.parametrize(
    "kind",
    [
        "llm_timeout",
        "llm_http_error",
        "llm_malformed_json",
        "llm_empty_response",
        "tool_args_corrupt",
        "tool_wrong_name",
        "tool_args_schema_violation",
        "tool_loop",
    ],
)
def test_every_llm_mutation_logs_before_return_and_preserves_input(
    store: TraceStore, kind: str
) -> None:
    plan = make_plan(kind)
    runtime = runtime_for(store, plan)
    source = tool_exchange(runtime.run.trace_id)
    before = source.model_dump()
    changed = runtime.exchange(source)
    rows = store.faults(runtime.run.id)
    assert len(rows) == 1 and rows[0].fault_class == kind and rows[0].target_seq == 0
    assert rows[0].created_at == 123.0 and rows[0].params == plan.faults[0].params
    assert store.get_run(runtime.run.id).fault_plan_id == plan.digest
    assert source.model_dump() == before and changed is not source
    if kind == "llm_timeout":
        assert changed.status_code == 504 and changed.response["error"]["type"] == kind
    elif kind == "llm_http_error":
        assert changed.status_code == 500
    elif kind == "llm_malformed_json":
        assert changed.response is None
        with pytest.raises(json.JSONDecodeError):
            json.loads(changed.error)
    elif kind == "llm_empty_response":
        assert changed.response["choices"] == []
    elif kind == "tool_args_corrupt":
        with pytest.raises(json.JSONDecodeError):
            json.loads(changed.tool_calls()[0].arguments)
    elif kind == "tool_wrong_name":
        assert changed.tool_calls()[0].name == "__undefined_tool__"
        collision = mutate_exchange(
            source, make_plan("tool_wrong_name", params={"name": "add"}).faults[0]
        )
        assert collision.tool_calls()[0].name == "add_"
    elif kind == "tool_args_schema_violation":
        arguments = json.loads(changed.tool_calls()[0].arguments)
        schema = source.request["tools"][0]["function"]["parameters"]
        assert not jsonschema.Draft202012Validator(schema).is_valid(arguments)
    else:
        calls = changed.tool_calls()
        assert len(calls) == 5 and len({call.id for call in calls}) == 5
        assert len({(call.name, call.arguments) for call in calls}) == 1


@pytest.mark.parametrize("mode", ["record", "replay"])
@pytest.mark.parametrize(
    "kind", ["tool_exec_timeout", "tool_exec_error", "tool_bad_result"]
)
def test_tool_faults_both_modes_record_actual_outcome_and_no_replay_execution(
    store: TraceStore,
    mode: str,
    kind: str,
) -> None:
    trace = store.create_trace(Trace(name="tool-fault"))
    calls = 0

    def real() -> int:
        nonlocal calls
        calls += 1
        return 5

    if mode == "replay":
        ToolRecorder(store, trace.id, "record").call("read", {}, real)
    params = (
        {"schema": {"type": "integer"}, "value": "bad"}
        if kind == "tool_bad_result"
        else {}
    )
    recorder = ToolRecorder(
        store,
        trace.id,
        mode,
        faults=make_plan(kind, target="tool_event", params=params),
    )
    if kind == "tool_bad_result":
        assert recorder.call("read", {}, real) == "bad"
    else:
        expected = TimeoutError if kind == "tool_exec_timeout" else RuntimeError
        with pytest.raises(expected, match="injected"):
            recorder.call("read", {}, real)
    runtime = recorder.faults
    assert len(store.faults(runtime.run.id)) == 1 and calls == 1
    observed = runtime.observed_tools[0]
    source = store.tool_events(trace.id)[0]
    if kind == "tool_bad_result":
        assert observed.result == "bad" and observed.error is None
    else:
        assert (
            observed.error.startswith(expected.__name__ + ":")
            and observed.result is None
        )
    assert (
        source == observed
        if mode == "record"
        else source.result == 5 and source.error is None
    )

    if kind == "tool_bad_result" and mode == "record":
        tuple_trace = store.create_trace(Trace(name="json-array"))
        tuple_recorder = ToolRecorder(
            store,
            tuple_trace.id,
            "record",
            faults=make_plan(
                kind,
                target="tool_event",
                params={"schema": {"type": "array"}, "value": "bad"},
            ),
        )
        assert tuple_recorder.call("tuple", {}, lambda: (1, 2)) == "bad"
        assert len(store.faults(tuple_recorder.faults.run.id)) == 1


def test_tool_targeted_loop_is_logged_when_next_llm_response_is_mutated(
    store: TraceStore,
) -> None:
    runtime = runtime_for(store, make_plan("tool_loop", target="tool_event"))
    event = ToolEvent(
        trace_id=runtime.run.trace_id,
        seq=0,
        name="add",
        args={"a": 1},
        args_hash=tool_args_hash({"a": 1}),
        result=1,
    )
    assert runtime(event, 1) == 1 and store.faults(runtime.run.id) == []
    assert runtime.pending_loops == [0]
    source = Exchange.from_request(
        runtime.run.trace_id, 1, chat_req(), status_code=200, response=chat_resp("done")
    )
    changed = runtime.exchange(source)
    assert len(changed.tool_calls()) == 5 and runtime.pending_loops == []
    row = store.faults(runtime.run.id)[0]
    assert (
        row.target == "tool_event"
        and row.target_seq == 0
        and row.fault_class == "tool_loop"
    )


def test_selection_reproducibility_is_asserted_on_fault_rows(store: TraceStore) -> None:
    trace = store.create_trace(Trace(name="repeat-plan"))
    plan = make_plan("llm_empty_response", select={"probability": 0.35})
    for seq in range(30):
        store.add_exchange(tool_exchange(trace.id, seq))
    before = canonical_json(
        [exchange.model_dump() for exchange in store.exchanges(trace.id)]
    )
    normalized = []
    for attempt in range(2):
        runtime = runtime_for(store, plan, trace.id)
        exchanges = store.exchanges(trace.id)
        if attempt:
            exchanges.reverse()
        for exchange in exchanges:
            runtime.exchange(exchange)
        rows = [
            row.model_dump(exclude={"id", "run_id", "created_at"})
            for row in store.faults(runtime.run.id)
        ]
        normalized.append(rows)
        assert rows and len(rows) < 30
    assert normalized[0] == normalized[1]
    assert (
        canonical_json(
            [exchange.model_dump() for exchange in store.exchanges(trace.id)]
        )
        == before
    )


def test_ineligible_mutations_are_skipped_and_first_applicable_rule_wins(
    store: TraceStore,
) -> None:
    plan = FaultPlan.from_dict(
        {
            "version": 1,
            "seed": 1,
            "faults": [
                {
                    "class": "tool_args_corrupt",
                    "target": "exchange",
                    "select": {"seq": [0]},
                },
                {
                    "class": "llm_http_error",
                    "target": "exchange",
                    "select": {"seq": [0]},
                    "params": {"status": 429},
                },
            ],
        }
    )
    runtime = runtime_for(store, plan)
    source = Exchange.from_request(
        runtime.run.trace_id, 0, chat_req(), status_code=200, response=chat_resp()
    )
    assert runtime.pre_exchange(source) is None
    assert runtime.exchange(source).status_code == 429
    assert (
        len(store.faults(runtime.run.id)) == 1
        and runtime.skipped[0]["class"] == "tool_args_corrupt"
    )
    assert invalid_arguments({}) is None
    with pytest.raises(FaultPlanError, match="external"):
        invalid_arguments({"$ref": "https://invalid.test/schema"})


def test_logging_failure_prevents_faulted_value_delivery(
    store: TraceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runtime_for(store, make_plan("llm_empty_response"))

    def fail_write(fault: Fault) -> None:
        raise OSError("storage unavailable")

    monkeypatch.setattr(store, "add_fault", fail_write)
    with pytest.raises(OSError, match="storage unavailable"):
        runtime.exchange(tool_exchange(runtime.run.trace_id))
    assert runtime.observed_exchanges == []


def test_fault_store_reopens_with_canonical_params_and_runtime_rejects_hash_mismatch(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "fault.db")
    plan = make_plan("llm_empty_response")
    runtime = runtime_for(store, plan)
    runtime.exchange(tool_exchange(runtime.run.trace_id))
    rows = store.faults(runtime.run.id)
    assert store.conn.execute("SELECT params FROM faults").fetchone()[
        0
    ] == canonical_json(rows[0].params)
    store.close()
    reopened = TraceStore(tmp_path / "fault.db")
    assert reopened.faults(runtime.run.id) == rows
    bad = runtime.run.model_copy(update={"fault_plan_id": "bad"})
    with pytest.raises(FaultPlanError, match="does not match"):
        FaultRuntime(reopened, bad)
    with pytest.raises(ValueError, match="unknown tool event"):
        reopened.update_tool_event(
            ToolEvent(
                trace_id=runtime.run.trace_id,
                seq=99,
                name="missing",
                args={},
                args_hash="x",
            )
        )
    reopened.close()


def test_finished_runtime_rejects_injection_before_executing_tool(
    store: TraceStore,
) -> None:
    runtime = runtime_for(store, make_plan("tool_exec_error", target="tool_event"))
    store.finish_run(runtime.run.id, "completed", finished_at=5.0)
    with pytest.raises(FaultPlanError, match="finished"):
        runtime.exchange(tool_exchange(runtime.run.trace_id))
    # A record tool cannot be bound to a replay runtime either.
    with pytest.raises(FaultPlanError, match="active"):
        ToolRecorder(store, runtime.run.trace_id, "record", faults=runtime)
    assert store.faults(runtime.run.id) == []
    before = len(store.runs(runtime.run.trace_id))
    with pytest.raises(FaultPlanError, match="shared session"):
        ToolRecorder(store, runtime.run.trace_id, "replay", faults=runtime)
    assert len(store.runs(runtime.run.trace_id)) == before
    record = ToolRecorder(
        store,
        runtime.run.trace_id,
        "record",
        faults=make_plan("tool_exec_error", target="tool_event"),
    )
    store.finish_run(record.faults.run.id, "completed", finished_at=6.0)
    with pytest.raises(FaultPlanError, match="finished"):
        record.call("late", {}, lambda: pytest.fail("executed after finish"))
    assert store.tool_events(runtime.run.trace_id) == []


def test_natural_tool_errors_are_observed_without_false_injection_labels(
    store: TraceStore,
) -> None:
    trace = store.create_trace(Trace(name="base-error"))
    recorder = ToolRecorder(
        store,
        trace.id,
        "record",
        faults=make_plan("tool_exec_error", target="tool_event"),
    )

    def fail() -> None:
        raise ValueError("natural failure")

    with pytest.raises(ValueError, match="natural failure"):
        recorder.call("fail", {}, fail)
    assert recorder.faults.observed_tools[0].error == "ValueError: natural failure"
    assert store.faults(recorder.faults.run.id) == []
