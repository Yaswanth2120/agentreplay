"""Logical evaluation slots and explicit attempt history; no database writes."""

from __future__ import annotations

from ..schema import Run

JobKey = tuple[
    str, str | None, int | None, str | None, str | None, int | None, str | None
]


def job_key(run: Run) -> JobKey:
    if run.config.get("phase") not in {
        "record",
        "clean_replay",
        "regression_replay",
        "fault_replay",
    }:
        raise ValueError("incomplete evaluation metadata: missing or unknown phase")
    return (
        run.config["phase"],
        run.task_id,
        run.trial,
        run.agent_version,
        run.config.get("source_run_id"),
        run.config.get("repeat"),
        run.fault_plan_id if run.config["phase"] == "fault_replay" else None,
    )


def index_runs(runs: list[Run]) -> dict[JobKey, Run]:
    result: dict[JobKey, Run] = {}
    seen = set()
    for run in runs:
        key = job_key(run)
        attempt = run.config.get("attempt", 0)
        if type(attempt) is not int or attempt < 0 or (key, attempt) in seen:
            raise ValueError("invalid or duplicate logical evaluation attempt")
        seen.add((key, attempt))
        if key not in result or result[key].config.get("attempt", 0) < attempt:
            result[key] = run
    return result


def effective_runs(runs: list[Run]) -> list[Run]:
    latest = index_runs(runs)
    return [run for run in runs if latest[job_key(run)].id == run.id]
