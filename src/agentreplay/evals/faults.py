"""M5 fixed-trajectory fault exercise, not an agent or detector score."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..faults import FaultPlan
from ..proxy import create_app
from ..schema import Trace
from ..store import TraceStore
from ..tools import ToolRecorder, ToolReplayedError


async def evaluate_trace(
    store: TraceStore,
    trace: Trace,
    plan: FaultPlan,
    *,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    app = create_app(
        store,
        "replay",
        trace_id=trace.id,
        faults=plan,
        run_config={"fault_evaluation": "fixed-recorded-trajectory"},
        wall_clock=clock,
    )
    session = app.state.session
    recorder = ToolRecorder(store, trace.id, "replay", session=session)
    exchanges = store.exchanges(trace.id)
    events = store.tool_events(trace.id)
    trajectory = sorted(
        [(exchange.seq, 0, exchange) for exchange in exchanges]
        + [(event.seq, 1, event) for event in events],
        key=lambda item: item[:2],
    )
    tool_errors = []
    completed = False
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://fault-eval",
            ) as client,
        ):
            for seq, kind, event in trajectory:
                if kind == 0:
                    await client.post("/v1/chat/completions", json=event.request)
                else:
                    try:
                        recorder.call(event.name, event.args, lambda **kwargs: None)
                    except (
                        TimeoutError,
                        RuntimeError,
                        ValueError,
                        TypeError,
                        OSError,
                        KeyError,
                        ToolReplayedError,
                    ) as error:
                        tool_errors.append(
                            {"seq": seq, "error": f"{type(error).__name__}: {error}"}
                        )
        completed = True
    finally:
        pending = sys.exc_info()[1]
        outcome = "completed" if completed else "agent_error"
        if not completed and isinstance(
            pending, (KeyboardInterrupt, SystemExit, GeneratorExit)
        ):
            outcome = "aborted"
        detail = (
            "fixed recorded trajectory; no scoring"
            if completed
            else f"{type(pending).__name__}: {pending}"
        )
        app.state.runs.finish(session.run.id, outcome, detail=detail)
    runtime = session.faults
    return {
        "run_id": session.run.id,
        "trace_id": trace.id,
        "fault_plan_id": plan.digest,
        "faults": [fault.model_dump() for fault in store.faults(session.run.id)],
        "findings": [finding.model_dump() for finding in store.findings(session.run.id)],
        "exchanges": [exchange.model_dump() for exchange in runtime.observed_exchanges],
        "tools": [event.model_dump() for event in runtime.observed_tools],
        "tool_errors": tool_errors,
        "skipped": runtime.skipped,
        "pending_loops": runtime.pending_loops,
    }


def evaluate_faults(
    store: TraceStore,
    plan: FaultPlan,
    trace_id: str | None = None,
    *,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    traces = (
        [store.get_trace(trace_id)] if trace_id is not None else store.list_traces()
    )
    if not traces or any(trace is None for trace in traces):
        raise ValueError("fault evaluation requires an existing recorded trace")
    return {
        "execution": "fixed-recorded-trajectory",
        "scored": False,
        "runs": [
            asyncio.run(evaluate_trace(store, trace, plan, clock=clock))
            for trace in traces
        ],
    }
