import ast
import copy
import inspect
from typing import Any

import pytest
from conftest import chat_req, chat_resp
from pydantic import ValidationError

from agentreplay import detectors
from agentreplay.canonical import tool_args_hash
from agentreplay.detectors import (
    detect,
    detect_budget_exceeded,
    detect_invalid_tool_call,
    detect_llm_error,
    detect_loop,
    detect_malformed_response,
    detect_timeout,
    detect_tool_error,
)
from agentreplay.json_validation import parse_arguments, schema_errors
from agentreplay.schema import (
    DetectorConfig,
    DetectorRun,
    Exchange,
    Run,
    RunView,
    ToolEvent,
)

SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}},
    "required": ["a"],
    "additionalProperties": False,
}


def exchange(
    seq: int = 0,
    *,
    status: int = 200,
    latency: float = 0.0,
    response: dict[str, Any] | None = None,
) -> Exchange:
    return Exchange.from_request(
        "trace",
        seq,
        chat_req(
            tools=[
                {
                    "type": "function",
                    "function": {"name": "add", "parameters": SCHEMA},
                }
            ]
        ),
        status_code=status,
        latency_ms=latency,
        response=copy.deepcopy(response if response is not None else chat_resp("done")),
    )


def tool_event(
    seq: int = 0,
    *,
    name: str = "add",
    args: dict[str, Any] | None = None,
    error: str | None = None,
    latency: float = 0.0,
) -> ToolEvent:
    args = args if args is not None else {"a": 1}
    return ToolEvent(
        trace_id="trace",
        seq=seq,
        name=name,
        args=args,
        args_hash=tool_args_hash(args),
        result=1,
        error=error,
        latency_ms=latency,
    )


def view(
    exchanges: list[Exchange] | None = None,
    tools: list[ToolEvent] | None = None,
    **config: Any,
) -> RunView:
    return RunView(
        run=DetectorRun(id="run", trace_id="trace", kind="replay"),
        exchanges=exchanges or [],
        tool_events=tools or [],
        config=DetectorConfig(**config),
        evaluated_at=123.0,
    )


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("unknown", '{"a":1}'),
        ("add", '{"a":'),
        ("add", "null"),
        ("add", "[]"),
        ("add", '{"a":"wrong"}'),
        ("add", "{}"),
        ("add", '{"a":1,"extra":2}'),
        ("add", '{"a":NaN}'),
        ("add", {"a": 1}),
        (None, '{"a":1}'),
    ],
)
def test_invalid_tool_calls_have_exchange_sequence_and_reason(
    name: Any, arguments: Any
) -> None:
    response = chat_resp(None, tool=("add", '{"a":1}'))
    response["choices"][0]["message"]["tool_calls"][0]["function"] = {
        "name": name,
        "arguments": arguments,
    }
    findings = detect_invalid_tool_call(view([exchange(seq=7, response=response)]))
    assert len(findings) == 1 and findings[0].kind == "invalid_tool_call"
    assert findings[0].exchange_seq == 7 and findings[0].evidence["reason"]


@pytest.mark.parametrize("arguments", ['{"a":0}', '{"a":1}', '{"a":-1}'])
def test_valid_tool_calls_are_accepted_at_integer_boundaries(arguments: str) -> None:
    assert (
        detect_invalid_tool_call(
            view([exchange(response=chat_resp(None, tool=("add", arguments)))])
        )
        == []
    )
    assert detect_invalid_tool_call(view([exchange()])) == []


def test_invalid_call_shapes_and_call_indices_are_handled() -> None:
    response = chat_resp(None, tool=("add", '{"a":1}'))
    response["choices"][0]["message"]["tool_calls"] = [
        None,
        {},
        {"function": {"name": "unknown", "arguments": "{}"}},
    ]
    findings = detect_invalid_tool_call(view([exchange(response=response)]))
    assert [finding.evidence["call_index"] for finding in findings] == [0, 1, 2]
    response["choices"][0]["message"]["tool_calls"] = {
        "function": {"name": "add", "arguments": '{"a":1}'}
    }
    assert detect_invalid_tool_call(view([exchange(response=response)]))


def test_schema_validation_supports_local_refs_and_rejects_invalid_unresolved_or_external_schemas() -> (
    None
):
    schema = {"$defs": {"args": SCHEMA}, "$ref": "#/$defs/args"}
    assert schema_errors({"a": 1}, schema) == []
    assert schema_errors({"a": "bad"}, schema)
    assert schema_errors({}, {"type": "unknown"})
    assert schema_errors({}, {"$ref": "#/$defs/missing"})
    assert (
        "external" in schema_errors({}, {"$ref": "https://no-network.test/schema"})[0]
    )
    assert parse_arguments('{"a":1}') == {"a": 1}
    with pytest.raises(TypeError, match="JSON object"):
        parse_arguments("[]")


@pytest.mark.parametrize(
    "count,expected", [(0, False), (1, False), (2, False), (3, True), (4, True)]
)
def test_consecutive_loop_boundary(count: int, expected: bool) -> None:
    findings = detect_loop(view(tools=[tool_event(index) for index in range(count)]))
    assert bool(findings) is expected
    if expected:
        assert len(findings) == 1 and findings[0].tool_seq == 0
        assert findings[0].evidence["cycle_length"] == 1


@pytest.mark.parametrize("period", [2, 3, 4])
@pytest.mark.parametrize("repeats,expected", [(2, False), (3, True)])
def test_cycle_lengths_and_repeat_boundary(
    period: int, repeats: int, expected: bool
) -> None:
    calls = [
        tool_event(index, name=f"tool-{index % period}")
        for index in range(period * repeats)
    ]
    findings = detect_loop(view(tools=calls))
    assert bool(findings) is expected
    if expected:
        assert findings[0].evidence["cycle_length"] == period


def test_cycles_outside_supported_size_or_window_are_not_loops() -> None:
    five = [tool_event(index, name=f"tool-{index % 5}") for index in range(15)]
    assert detect_loop(view(tools=five)) == []
    four = [tool_event(index, name=f"tool-{index % 4}") for index in range(12)]
    assert detect_loop(view(tools=four, loop_window=11)) == []
    assert detect_loop(view(tools=four, loop_window=12))
    interrupted = [
        tool_event(index, name=name)
        for index, name in enumerate(["a", "a", "b", "a", "a"])
    ]
    assert detect_loop(view(tools=interrupted)) == []
    assert detect_loop(view(tools=[tool_event(0), tool_event(1)], loop_repeats=2))


def test_emitted_calls_are_not_double_counted_with_executions() -> None:
    exchanges = [
        exchange(index, response=chat_resp(None, tool=("add", '{"a":1}')))
        for index in range(2)
    ]
    events = [tool_event(index) for index in range(2)]
    assert detect_loop(view(exchanges, events)) == []
    calls = exchanges[-1].response["choices"][0]["message"]["tool_calls"]
    calls.append(copy.deepcopy(calls[0]))
    finding = detect_loop(view(exchanges, events))[0]
    assert finding.exchange_seq == 1 and finding.tool_seq == 0


@pytest.mark.parametrize(
    "latency,expected", [(59999.0, False), (60000.0, False), (60000.1, True)]
)
def test_timeout_latency_boundary_for_exchanges_and_tools(
    latency: float, expected: bool
) -> None:
    findings = detect_timeout(
        view([exchange(seq=4, latency=latency)], [tool_event(seq=8, latency=latency)])
    )
    assert len(findings) == (2 if expected else 0)
    if expected:
        assert findings[0].exchange_seq == 4 and findings[1].tool_seq == 8


@pytest.mark.parametrize(
    "status,timeout,llm_error",
    [
        (200, False, False),
        (399, False, False),
        (400, False, True),
        (429, False, True),
        (500, False, True),
        (504, True, False),
        (599, True, False),
    ],
)
def test_status_timeout_and_llm_error_classification(
    status: int, timeout: bool, llm_error: bool
) -> None:
    observed = view([exchange(status=status)])
    assert bool(detect_timeout(observed)) is timeout
    assert bool(detect_llm_error(observed)) is llm_error
    assert detect_llm_error(view([exchange(status=status, latency=60001.0)])) == []


def test_timeout_configuration_and_tool_exception_text_are_separate() -> None:
    assert detect_timeout(view(tools=[tool_event(latency=101.0)], timeout_ms=100.0))
    assert (
        detect_timeout(
            view(
                tools=[tool_event(latency=101.0)],
                timeout_ms=100.0,
                tool_timeout_ms=200.0,
            )
        )
        == []
    )
    observed = view(tools=[tool_event(error="TimeoutError: injected", latency=0.0)])
    assert detect_timeout(observed) == [] and detect_tool_error(observed)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": None},
        {"choices": []},
        {"choices": "wrong"},
        {"choices": [None]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": None, "tool_calls": []}}]},
        {"choices": [{"message": {"content": 5}}]},
    ],
)
def test_malformed_response_positive_shapes(response: dict[str, Any]) -> None:
    findings = detect_malformed_response(view([exchange(seq=3, response=response)]))
    assert len(findings) == 1 and findings[0].exchange_seq == 3
    # Other detectors must also tolerate malformed completion shapes.
    assert isinstance(detect(view([exchange(response=response)])), list)


def test_unparseable_response_and_valid_message_boundaries() -> None:
    missing = exchange().model_copy(update={"response": None, "error": '{"truncated":'})
    assert detect_malformed_response(view([missing]))
    for content in ["", "ok"]:
        assert (
            detect_malformed_response(view([exchange(response=chat_resp(content))]))
            == []
        )
    assert (
        detect_malformed_response(
            view([exchange(response=chat_resp(None, tool=("add", '{"a":1}')))])
        )
        == []
    )
    for status in [429, 500, 504, 599]:
        assert (
            detect_malformed_response(
                view(
                    [
                        exchange(
                            status=status,
                            response={"error": {"message": "valid HTTP error"}},
                        )
                    ]
                )
            )
            == []
        )


@pytest.mark.parametrize(
    "error,expected", [(None, False), ("", True), ("ValueError: bad", True)]
)
def test_tool_error_presence_boundary(error: str | None, expected: bool) -> None:
    findings = detect_tool_error(view(tools=[tool_event(seq=9, error=error)]))
    assert bool(findings) is expected
    if expected:
        assert findings[0].tool_seq == 9 and findings[0].kind == "tool_error"


@pytest.mark.parametrize(
    "count,expected", [(0, False), (24, False), (25, False), (26, True)]
)
def test_budget_boundary(count: int, expected: bool) -> None:
    findings = detect_budget_exceeded(view([exchange(index) for index in range(count)]))
    assert bool(findings) is expected
    if expected:
        assert findings[0].severity == "warning" and findings[0].exchange_seq == 25
        assert findings[0].evidence == {"exchanges": 26, "max_steps": 25}


def test_budget_includes_unmatched_replay_attempts_and_respects_configuration() -> None:
    observed = view([], max_steps=2)
    observed.exchange_attempts = 3
    assert detect_budget_exceeded(observed)[0].evidence["exchanges"] == 3
    assert detect_budget_exceeded(view([exchange(0)], max_steps=0))


@pytest.mark.parametrize(
    "config",
    [
        {"timeout_ms": -1.0},
        {"timeout_ms": float("nan")},
        {"tool_timeout_ms": -1.0},
        {"max_steps": -1},
        {"max_steps": True},
        {"loop_repeats": 1},
        {"max_cycle_length": 5},
        {"loop_window": 2},
        {"unknown": 1},
    ],
)
def test_invalid_threshold_configuration_is_rejected(config: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DetectorConfig(**config)


def test_detector_module_is_label_storage_and_clock_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_run = Run(
        id="run",
        kind="replay",
        trace_id="trace",
        fault_plan_id="secret-label",
        config={"fault_plan": {"class": "timeout"}},
    )
    observed = RunView(
        run=raw_run, exchanges=[exchange(status=504)], evaluated_at=123.0
    )
    assert (
        "config" not in observed.run.model_dump()
        and "fault_plan_id" not in observed.run.model_dump()
    )
    before = observed.model_dump()

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("pure detection read time or generated a random ID")

    monkeypatch.setattr("time.time", forbidden)
    monkeypatch.setattr("uuid.uuid4", forbidden)
    first = detect(observed)
    assert first == detect(observed) and observed.model_dump() == before
    assert first[0].created_at == 123.0 and len(first[0].id) == 64
    tree = ast.parse(inspect.getsource(detectors))
    imports = [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ]
    assert all(
        module is None
        or not module.startswith(
            ("store", "faults", "fault_runtime", "httpx", "requests")
        )
        for module in imports
    )
    for name in ("store", "faults", "fault_runtime"):
        assert name not in detectors.__dict__
