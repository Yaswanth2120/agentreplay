"""Validated, canonical JSONL trace export/import; SQL belongs to the store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .canonical import (
    LENIENT_KEYS,
    STRICT_KEYS,
    canonical_hash,
    canonical_json,
    tool_args_hash,
)
from .schema import (
    EXPORT_FORMAT,
    SCHEMA_VERSION,
    Exchange,
    ExchangeRecord,
    ToolEvent,
    ToolRecord,
    Trace,
    TraceRecord,
)
from .store import TraceStore


class TraceFormatError(ValueError):
    """Invalid or incompatible trace JSONL, with a line number when possible."""


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def _parse_line(text: str, number: int) -> TraceRecord | ExchangeRecord | ToolRecord:
    try:
        value = json.loads(text, parse_constant=_reject_constant)
        if not isinstance(value, dict):
            raise TypeError("expected a JSON object")
        record_type = value.get("type")
        models = {
            "trace": TraceRecord,
            "exchange": ExchangeRecord,
            "tool_event": ToolRecord,
        }
        if not isinstance(record_type, str) or record_type not in models:
            raise ValueError(f"unknown record type {record_type!r}")
        model = models[record_type]
        missing = set(model.model_fields) - value.keys()
        if missing:
            raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
        return model.model_validate(value)
    except (ValueError, TypeError) as error:
        raise TraceFormatError(f"line {number}: {error}") from error


def load_trace(path: str | Path) -> tuple[Trace, list[Exchange], list[ToolEvent]]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except UnicodeError as error:
        raise TraceFormatError(f"trace file is not UTF-8: {error}") from error
    if not lines:
        raise TraceFormatError("line 1: missing trace header")
    header = _parse_line(lines[0], 1)
    if not isinstance(header, TraceRecord):
        raise TraceFormatError("line 1: first record must be a trace header")
    if header.schema_version != SCHEMA_VERSION:
        raise TraceFormatError(
            f"line 1: schema v{header.schema_version}, expected v{SCHEMA_VERSION}"
        )
    trace = Trace(**header.model_dump(exclude={"type", "format"}))
    exchanges: list[Exchange] = []
    events: list[ToolEvent] = []
    previous = {"exchange": -1, "tool_event": -1}
    seen: set[tuple[str, str]] = set()
    for number, text in enumerate(lines[1:], 2):
        record = _parse_line(text, number)
        if isinstance(record, TraceRecord):
            raise TraceFormatError(f"line {number}: duplicate trace header")
        if record.trace_id != trace.id:
            raise TraceFormatError(f"line {number}: trace_id does not match header")
        key = (record.type, record.id)
        if key in seen or record.seq <= previous[record.type]:
            raise TraceFormatError(
                f"line {number}: duplicate ID or unordered/duplicate sequence"
            )
        seen.add(key)
        previous[record.type] = record.seq
        if isinstance(record, ExchangeRecord):
            if record.strict_hash != canonical_hash(
                record.request, STRICT_KEYS
            ) or record.lenient_hash != canonical_hash(record.request, LENIENT_KEYS):
                raise TraceFormatError(f"line {number}: request hash mismatch")
            exchanges.append(Exchange(**record.model_dump(exclude={"type"})))
        else:
            if record.args_hash != tool_args_hash(record.args):
                raise TraceFormatError(f"line {number}: tool argument hash mismatch")
            if record.error is not None and record.result is not None:
                raise TraceFormatError(
                    f"line {number}: failed tool event cannot also have a result"
                )
            events.append(ToolEvent(**record.model_dump(exclude={"type"})))
    return trace, exchanges, events


def export_trace(store: TraceStore, trace_id: str, path: str | Path) -> None:
    trace = store.get_trace(trace_id)
    if trace is None:
        raise TraceFormatError(f"unknown trace {trace_id!r}")
    header = {"type": "trace", "format": EXPORT_FORMAT, **trace.model_dump()}
    records: list[dict[str, Any]] = [
        {"type": "exchange", **exchange.model_dump()}
        for exchange in store.exchanges(trace_id)
    ]
    records.extend(
        {"type": "tool_event", **event.model_dump()}
        for event in store.tool_events(trace_id)
    )
    records.sort(key=lambda record: (record["seq"], record["type"] != "exchange"))
    text = "\n".join(canonical_json(record) for record in [header, *records]) + "\n"
    Path(path).write_text(text, encoding="utf-8")


def import_trace(store: TraceStore, path: str | Path) -> Trace:
    trace, exchanges, events = load_trace(path)
    try:
        store.import_trace(trace, exchanges, events)
    except sqlite3.IntegrityError as error:
        raise TraceFormatError(
            f"trace import conflicts with existing rows: {error}"
        ) from error
    return trace
