"""Readable trace inspection, separate from argparse and SQL."""

from __future__ import annotations

import json

from .replay import json_diff
from .store import TraceStore


def render_list(store: TraceStore) -> str:
    lines = []
    for trace in store.list_traces():
        lines.append(
            f"{trace.id}  {trace.name}  "
            f"{len(store.exchanges(trace.id))} exchanges  "
            f"{len(store.tool_events(trace.id))} tool events  "
            f"{len(store.runs(trace.id))} runs"
        )
    return "\n".join(lines) if lines else "No traces."


def render_trace(store: TraceStore, trace_id: str) -> str:
    trace = store.get_trace(trace_id)
    if trace is None:
        raise ValueError(f"unknown trace {trace_id!r}")
    lines = [
        f"Trace {trace.id}: {trace.name}",
        json.dumps(trace.model_dump(), ensure_ascii=False, sort_keys=True, indent=2),
    ]
    for exchange in store.exchanges(trace_id):
        lines.append(
            f"Exchange {exchange.seq}: HTTP {exchange.status_code}, stream={exchange.stream}"
        )
        lines.append(
            json.dumps(
                exchange.model_dump(), ensure_ascii=False, sort_keys=True, indent=2
            )
        )
    for event in store.tool_events(trace_id):
        lines.append(f"Tool {event.seq}: {event.name}")
        lines.append(
            json.dumps(event.model_dump(), ensure_ascii=False, sort_keys=True, indent=2)
        )
    for run in store.runs(trace_id):
        lines.append(f"Run {run.id}: {run.kind}, outcome={run.outcome or 'unfinished'}")
        lines.append(
            json.dumps(run.model_dump(), ensure_ascii=False, sort_keys=True, indent=2)
        )
        for finding in store.findings(run.id):
            lines.append(f"Finding: {finding.severity} {finding.kind}")
            lines.append(
                json.dumps(
                    finding.model_dump(), ensure_ascii=False, sort_keys=True, indent=2
                )
            )
    return "\n".join(lines)


def render_run_diff(store: TraceStore, run_a: str, run_b: str) -> str:
    first, second = store.get_run(run_a), store.get_run(run_b)
    if first is None or second is None:
        raise ValueError("both run IDs must exist")
    before = {
        exchange.seq: exchange.request for exchange in store.exchanges(first.trace_id)
    }
    after = {
        exchange.seq: exchange.request for exchange in store.exchanges(second.trace_id)
    }
    changes = []
    for seq in sorted(before.keys() | after.keys()):
        diff = json_diff(before.get(seq), after.get(seq))
        if any(diff.values()):
            changes.append({"seq": seq, "diff": diff})
    return json.dumps(
        {
            "run_a": first.model_dump(),
            "run_b": second.model_dump(),
            "source_request_diffs": changes,
            "findings_a": [finding.model_dump() for finding in store.findings(run_a)],
            "findings_b": [finding.model_dump() for finding in store.findings(run_b)],
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
