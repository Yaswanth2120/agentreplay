"""Build sanitized observed views, run pure detectors, and persist their findings."""

from __future__ import annotations

from .detectors import DETECTOR_KINDS, detect
from .schema import (
    DetectorConfig,
    DetectorRun,
    Finding,
    Run,
    RunObservations,
    RunOutcome,
    RunView,
)
from .store import TraceStore


def make_run_view(
    run: Run, observations: RunObservations, config: DetectorConfig | None = None
) -> RunView:
    if any(
        event.trace_id != run.trace_id
        for event in [*observations.exchanges, *observations.tool_events]
    ):
        raise ValueError("observations must belong to the run's trace")
    metadata = run.model_dump(include=set(DetectorRun.model_fields))
    return RunView(
        run=DetectorRun(**metadata),
        exchanges=observations.exchanges,
        tool_events=observations.tool_events,
        exchange_attempts=observations.exchange_attempts,
        config=config or DetectorConfig.model_validate(run.config.get("detectors", {})),
        evaluated_at=run.finished_at if run.finished_at is not None else run.started_at,
    )


def load_run_view(
    store: TraceStore, run_id: str, config: DetectorConfig | None = None
) -> RunView:
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run {run_id!r}")
    if run.finished_at is None:
        raise ValueError("finish the run before persisted detection")
    observed = store.get_observations(run_id)
    if observed is None:
        if run.kind == "replay":
            raise ValueError(
                "replay has no observed snapshot; finish it through its session or run manager first"
            )
        observed = RunObservations(
            exchanges=store.exchanges(run.trace_id),
            tool_events=store.tool_events(run.trace_id),
        )
    return make_run_view(run, observed, config)


def detect_run(
    store: TraceStore, run_id: str, config: DetectorConfig | None = None
) -> list[Finding]:
    findings = detect(load_run_view(store, run_id, config))
    store.replace_detector_findings(run_id, findings, DETECTOR_KINDS)
    return findings


def finish_run(
    store: TraceStore,
    run: Run,
    outcome: RunOutcome,
    *,
    finished_at: float,
    observations: RunObservations,
    task_success: bool | None = None,
    detail: str | None = None,
    findings: list[Finding] | None = None,
) -> Run:
    finished = run.model_copy(
        update={
            "outcome": outcome,
            "finished_at": finished_at,
            "task_success": task_success,
            "detail": detail,
        }
    )
    detected = detect(make_run_view(finished, observations))
    return store.finish_run(
        run.id,
        outcome,
        finished_at=finished_at,
        task_success=task_success,
        detail=detail,
        findings=[*(findings or []), *detected],
        observations=observations,
    )
