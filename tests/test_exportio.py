import json
from pathlib import Path
from typing import Any

import pytest
from conftest import chat_req, chat_resp

from agentreplay.canonical import canonical_json, tool_args_hash
from agentreplay.exportio import (
    EXPORT_FORMAT,
    TraceFormatError,
    export_trace,
    import_trace,
    load_trace,
)
from agentreplay.schema import SCHEMA_VERSION, Exchange, ToolEvent, Trace
from agentreplay.store import TraceStore


def populated_trace(store: TraceStore) -> Trace:
    trace = store.create_trace(
        Trace(
            name="portable-é",
            task_id="t",
            agent_name="agent",
            agent_version="v1",
            model="mock",
            metadata={"z": 0.0, "a": {"nested": True}},
        )
    )
    for index in range(2):
        store.add_exchange(
            Exchange.from_request(
                trace.id,
                index,
                chat_req(str(index)),
                status_code=200,
                response=chat_resp("é"),
                stream=bool(index),
                chunks=['data: {"choices":[]}', "data: [DONE]"] if index else None,
                latency_ms=1.25,
            )
        )
    store.add_tool_event(
        ToolEvent(
            trace_id=trace.id,
            seq=0,
            name="read",
            args={"z": 0.0, "a": 1},
            args_hash=tool_args_hash({"z": 0.0, "a": 1}),
            result=None,
        )
    )
    store.add_tool_event(
        ToolEvent(
            trace_id=trace.id,
            seq=1,
            name="fail",
            args={},
            args_hash=tool_args_hash({}),
            error="ValueError: broken",
        )
    )
    return trace


def canonical_rows(store: TraceStore) -> bytes:
    rows: dict[str, Any] = {}
    for table in ("traces", "exchanges", "tool_events"):
        rows[table] = [
            dict(row)
            for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY id")
        ]
    return canonical_json(rows).encode("utf-8")


def test_export_import_preserves_canonical_rows_and_bytes(
    store: TraceStore, tmp_path: Path
) -> None:
    trace = populated_trace(store)
    first, second = tmp_path / "first.jsonl", tmp_path / "second.jsonl"
    export_trace(store, trace.id, first)
    fresh = TraceStore(tmp_path / "fresh.db")
    assert import_trace(fresh, first) == trace
    assert canonical_rows(fresh) == canonical_rows(store)
    assert fresh.exchanges(trace.id) == store.exchanges(trace.id)
    assert fresh.tool_events(trace.id) == store.tool_events(trace.id)
    assert fresh.runs(trace.id) == []
    export_trace(fresh, trace.id, second)
    assert first.read_bytes() == second.read_bytes()
    header, exchanges, events = load_trace(first)
    assert header == trace and len(exchanges) == len(events) == 2
    assert header.model == "mock" and header.agent_version == "v1"
    lines = [json.loads(line) for line in first.read_text().splitlines()]
    assert lines[0]["format"] == EXPORT_FORMAT
    assert [(line["type"], line["seq"]) for line in lines[1:]] == [
        ("exchange", 0),
        ("tool_event", 0),
        ("exchange", 1),
        ("tool_event", 1),
    ]
    assert all(
        line == canonical_json(json.loads(line))
        for line in first.read_text().splitlines()
    )
    assert (
        fresh.conn.execute(
            "SELECT result FROM tool_events WHERE name='read'"
        ).fetchone()[0]
        == "null"
    )
    assert (
        fresh.conn.execute(
            "SELECT result FROM tool_events WHERE name='fail'"
        ).fetchone()[0]
        is None
    )
    fresh.close()


@pytest.mark.parametrize(
    "problem",
    [
        "format",
        "schema",
        "malformed",
        "blank",
        "unknown_type",
        "extra_field",
        "missing_id",
        "wrong_trace",
        "wrong_hash",
        "wrong_tool_hash",
        "duplicate_header",
        "duplicate_seq",
        "duplicate_id",
        "negative_seq",
        "coerced_seq",
        "both_result_error",
        "nonfinite",
        "array",
    ],
)
def test_import_rejects_invalid_lines_without_partial_writes(
    store: TraceStore,
    tmp_path: Path,
    problem: str,
) -> None:
    trace = populated_trace(store)
    path = tmp_path / "invalid.jsonl"
    export_trace(store, trace.id, path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    line = 2
    if problem == "format":
        rows[0]["format"] = "agentreplay.trace.v99"
        line = 1
    elif problem == "schema":
        rows[0]["schema_version"] = SCHEMA_VERSION - 1
        line = 1
    elif problem == "unknown_type":
        rows[1]["type"] = "run"
    elif problem == "extra_field":
        rows[1]["Authorization"] = "not accepted"
    elif problem == "missing_id":
        del rows[1]["id"]
    elif problem == "wrong_trace":
        rows[1]["trace_id"] = "other"
    elif problem == "wrong_hash":
        rows[1]["strict_hash"] = "wrong"
    elif problem == "wrong_tool_hash":
        rows[2]["args_hash"] = "wrong"
        line = 3
    elif problem == "duplicate_header":
        rows[1] = rows[0]
    elif problem == "duplicate_seq":
        rows[3]["seq"] = 0
        line = 4
    elif problem == "duplicate_id":
        rows[3]["id"] = rows[1]["id"]
        line = 4
    elif problem == "negative_seq":
        rows[1]["seq"] = -1
    elif problem == "coerced_seq":
        rows[1]["seq"] = "0"
    elif problem == "both_result_error":
        rows[4]["result"] = {"not": "null"}
        line = 5
    elif problem == "nonfinite":
        rows[1]["latency_ms"] = float("nan")
    elif problem == "array":
        rows[1] = []
    text = [canonical_json(row) for row in rows]
    if problem == "malformed":
        text[1] = '{"broken":'
    if problem == "blank":
        text[1] = ""
    path.write_text("\n".join(text) + "\n")
    fresh = TraceStore()
    with pytest.raises(TraceFormatError, match=f"line {line}:"):
        import_trace(fresh, path)
    assert fresh.list_traces() == []
    assert fresh.conn.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0] == 0
    assert fresh.conn.execute("SELECT COUNT(*) FROM tool_events").fetchone()[0] == 0
    fresh.close()


@pytest.mark.parametrize("content", [b"", b"\xff\xfe", b'{"type":"nope"}\n'])
def test_invalid_headers_are_clear_and_leave_empty_database(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "header.jsonl"
    path.write_bytes(content)
    fresh = TraceStore()
    with pytest.raises(TraceFormatError):
        import_trace(fresh, path)
    assert fresh.list_traces() == []
    fresh.close()


def test_import_conflicts_roll_back_entire_trace(
    store: TraceStore, tmp_path: Path
) -> None:
    trace = populated_trace(store)
    path = tmp_path / "conflict.jsonl"
    export_trace(store, trace.id, path)
    fresh = TraceStore()
    existing = fresh.create_trace(Trace(name="existing"))
    conflict = store.exchanges(trace.id)[1].model_copy(update={"trace_id": existing.id})
    fresh.add_exchange(conflict)
    before = canonical_rows(fresh)
    with pytest.raises(TraceFormatError, match="conflicts"):
        import_trace(fresh, path)
    assert canonical_rows(fresh) == before
    assert fresh.get_trace(trace.id) is None
    assert fresh.tool_events(trace.id) == []
    with pytest.raises(TraceFormatError, match="conflicts"):
        import_trace(store, path)
    fresh.close()


def test_export_unknown_trace_does_not_truncate_output(
    store: TraceStore, tmp_path: Path
) -> None:
    path = tmp_path / "keep.jsonl"
    path.write_text("keep")
    with pytest.raises(TraceFormatError, match="unknown trace"):
        export_trace(store, "absent", path)
    assert path.read_text() == "keep"


def test_exchange_cannot_be_first_record(store: TraceStore, tmp_path: Path) -> None:
    trace = populated_trace(store)
    path = tmp_path / "no-header.jsonl"
    export_trace(store, trace.id, path)
    path.write_text("\n".join(path.read_text().splitlines()[1:]) + "\n")
    with pytest.raises(TraceFormatError, match="first record"):
        load_trace(path)
