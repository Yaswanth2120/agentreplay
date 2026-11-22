"""Collect manual presence judgments without showing detector output."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..schema import EvalLabel
from ..store import TraceStore


def blind_run(store: TraceStore, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None or run.finished_at is None:
        raise ValueError("manual labeling requires a finished run")
    if run.fault_plan_id is not None:
        raise ValueError(
            "manual labels must come from natural runs without injected faults"
        )
    observed = store.get_observations(run_id)
    if observed is None:
        if run.kind == "replay":
            raise ValueError("replay labeling requires observed snapshots")
        from ..schema import RunObservations

        observed = RunObservations(
            exchanges=store.exchanges(run.trace_id),
            tool_events=store.tool_events(run.trace_id),
        )
    return {
        "run_id": run.id,
        "task_id": run.task_id,
        "model": run.model,
        "exchanges": [exchange.model_dump() for exchange in observed.exchanges],
        "tool_events": [event.model_dump() for event in observed.tool_events],
    }


def label_run(
    store: TraceStore,
    run_id: str,
    *,
    labeler: str = "owner",
    ask: Callable[[str], str] = input,
    clock: Callable[[], float] = time.time,
) -> list[EvalLabel]:
    blind_run(store, run_id)
    labels = []
    for kind in ("loop", "invalid_tool_call", "timeout"):
        answer = ask(f"{kind} present? [y/n] ").strip().lower()
        if answer not in {"y", "n"}:
            raise ValueError("presence answer must be y or n; no labels saved")
        labels.append(
            EvalLabel(
                run_id=run_id,
                kind=kind,
                present=answer == "y",
                labeler=labeler,
                created_at=clock(),
            )
        )
    store.replace_labels(labels)
    return labels
