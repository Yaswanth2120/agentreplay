"""Load one complete evaluation cohort and render statistics computed by stats.py."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .evals.resume import effective_runs
from .evals.stats import summarize
from .faults import FaultPlan
from .store import TraceStore


def observed_call_count(exchanges: list[Any]) -> int:
    count = 0
    for exchange in exchanges:
        choices = (exchange.response or {}).get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            message = choice.get("message") if isinstance(choice, dict) else None
            calls = message.get("tool_calls") if isinstance(message, dict) else None
            if calls:
                count += len(calls) if isinstance(calls, list) else 1
    return count


def load_records(
    store: TraceStore, evaluation_id: str | None = None
) -> list[dict[str, Any]]:
    runs = [run for run in store.all_runs() if run.config.get("evaluation_id")]
    if not runs:
        raise ValueError("no evaluation runs to report")
    evaluation_id = evaluation_id or runs[-1].config["evaluation_id"]
    runs = effective_runs(
        [run for run in runs if run.config["evaluation_id"] == evaluation_id]
    )
    records = []
    for run in runs:
        if run.config["evaluation_id"] != evaluation_id:
            continue
        result, observations = (
            store.get_evaluation(run.id),
            store.get_observations(run.id),
        )
        if (
            run.finished_at is None
            or run.task_success is None
            or result is None
            or observations is None
        ):
            raise ValueError(
                "evaluation is incomplete; finished results and observations are required"
            )
        records.append(
            {
                "run": run.model_dump(),
                "result": result.model_dump(),
                "findings": [
                    finding.model_dump() for finding in store.findings(run.id)
                ],
                "faults": [fault.model_dump() for fault in store.faults(run.id)],
                "labels": [label.model_dump() for label in store.labels(run.id)],
                "call_count": observed_call_count(observations.exchanges),
                "steps": observations.exchange_attempts
                if observations.exchange_attempts is not None
                else len(observations.exchanges),
            }
        )
    if not records:
        raise ValueError(f"unknown evaluation {evaluation_id!r}")
    validate_cohort(records)
    return records


def validate_cohort(records: list[dict[str, Any]]) -> None:
    if not records or len({record["run"]["id"] for record in records}) != len(records):
        raise ValueError("evaluation run IDs must be nonempty and unique")
    first = records[0]["run"]["config"]
    config = first["evaluation_config"]
    shared = (
        "evaluation_config",
        "tasks_version",
        "git_sha",
        "vllm_version",
        "vllm_flags",
        "owner_review",
        "evaluation_started_at",
    )
    for record in records:
        metadata = record["run"]["config"]
        if any(metadata[key] != first[key] for key in shared):
            raise ValueError("evaluation metadata differs within one cohort")
    sources = {
        r["run"]["id"]: r for r in records if r["run"]["config"]["phase"] == "record"
    }
    keys = [
        (r["run"]["task_id"], r["run"]["trial"], r["run"]["agent_version"])
        for r in sources.values()
    ]
    expected = {
        (task, trial, version)
        for task in config["task_ids"]
        for trial in range(config["trials"])
        for version in config["versions"]
    }
    if len(keys) != len(expected) or set(keys) != expected:
        raise ValueError(
            "evaluation has missing or duplicate task/trial/version records"
        )
    for id, source in sources.items():
        children = [r for r in records if r["run"]["config"].get("source_run_id") == id]
        for child in children:
            run = child["run"]
            phase = run["config"]["phase"]
            wanted_version = (
                "v2" if phase == "regression_replay" else source["run"]["agent_version"]
            )
            if (
                any(
                    run[key] != source["run"][key]
                    for key in ("task_id", "trial", "trace_id")
                )
                or run["agent_version"] != wanted_version
            ):
                raise ValueError("replay metadata does not match its source")
        repeats = [r for r in children if r["run"]["config"]["phase"] == "clean_replay"]
        if sorted(r["run"]["config"]["repeat"] for r in repeats) != [0, 1]:
            raise ValueError("evaluation needs two complete replay repeats per source")
        clean = source["run"]["task_success"] is True and not source["findings"]
        if any(r["run"]["config"].get("clean_source") != clean for r in repeats):
            raise ValueError("clean replay eligibility differs from its source")
        baseline = source["run"]["agent_version"] == "v1"
        expected_regression = int(baseline and "v2" in config["versions"])
        plan_values = (
            [config["fault_plan"]]
            if config["fault_plan"] is not None
            else config.get("fault_plans", [])
        )
        plan_ids = {FaultPlan.from_dict(value).digest for value in plan_values}
        expected_fault = len(plan_ids) if baseline and clean else 0
        actual_plans = {
            r["run"]["fault_plan_id"]
            for r in children
            if r["run"]["config"]["phase"] == "fault_replay"
        }
        if actual_plans != (plan_ids if expected_fault else set()):
            raise ValueError("evaluation has incomplete fault plan coverage")
        if (
            sum(r["run"]["config"]["phase"] == "regression_replay" for r in children)
            != expected_regression
            or sum(r["run"]["config"]["phase"] == "fault_replay" for r in children)
            != expected_fault
        ):
            raise ValueError("evaluation has incomplete regression or fault replays")
    allowed = {"record", "clean_replay", "regression_replay", "fault_replay"}
    if any(
        r["run"]["config"]["phase"] not in allowed
        or (
            r["run"]["config"]["phase"] != "record"
            and r["run"]["config"].get("source_run_id") not in sources
        )
        for r in records
    ):
        raise ValueError("unknown evaluation phase or source")


def build_report(store: TraceStore, evaluation_id: str | None = None) -> dict[str, Any]:
    records = load_records(store, evaluation_id)
    metadata = records[0]["run"]["config"]
    settings = metadata["evaluation_config"]
    history = [
        run
        for run in store.all_runs()
        if run.config.get("evaluation_id") == metadata["evaluation_id"]
    ]
    selected_ids = {record["run"]["id"] for record in records}
    return {
        "config": {
            "evaluation_id": metadata["evaluation_id"],
            "model": settings["model"],
            "versions": settings["versions"],
            "tasks_version": metadata["tasks_version"],
            "git_sha": metadata["git_sha"],
            "date": datetime.fromtimestamp(
                metadata["evaluation_started_at"], timezone.utc
            ).isoformat(),
            "backend": settings["backend"],
            "task_ids": settings["task_ids"],
            "trials": settings["trials"],
            "temperature": 0.7,
            "vllm_version": metadata["vllm_version"],
            "vllm_flags": metadata["vllm_flags"],
            "owner_review": metadata["owner_review"],
            "model_revision": settings.get("model_revision"),
            "max_tokens": settings.get("max_tokens"),
            "chat_template_kwargs": settings.get("chat_template_kwargs", {}),
            "environments": [
                json.loads(value)
                for value in sorted(
                    {
                        canonical_json(run.config.get("environment", {}))
                        for run in history
                        if run.config.get("environment")
                    }
                )
            ],
            "superseded_attempts": [
                run.id for run in history if run.id not in selected_ids
            ],
        },
        "metrics": summarize(records, settings),
        "notes": [
            "Scripted offline results validate plumbing, not live model capability."
            if settings["backend"] == "scripted"
            else "vLLM seeded sampling is best-effort; replay provides reproducibility.",
            "Owner task hand-solve review is pending; no validated benchmark or significance claim is made.",
            "Wilson intervals use the displayed count unit; F1 uses conservative harmonic bounds from Bonferroni-adjusted Wilson intervals.",
            "Trial pairs are resampled as paired observations; repeated tasks/trials and replay repeats are correlated, so generalization is limited.",
            "Clean FP denominators include both repeats of successful, finding-free source runs. Determinism compares each source's two repeats.",
            "Task failure is run-level; bad-result faults without localized verification are counted as unscored, never invented detector true positives.",
            "Manual presence-label scores remain separate from injected sequence-label scores; missing denominators are shown as undefined.",
            "Natural tool failures currently gain a ToolReplayedError wrapper on replay, potentially causing unchanged-agent strict misses. Repeat determinism does not establish equivalence with the source recording.",
            "Clean FP rates are conditional on successful finding-free source runs, not estimates over all natural runs; independent human labels remain pending.",
            "Loop P/R includes exact target-sequence localization: an observed loop at a later exchange can count as FP/FN against its causal tool-event label even when loop presence was detected.",
        ],
    }


def validate_rates(value: Any) -> None:
    if isinstance(value, dict):
        if "value" in value:
            if type(value.get("n")) is not int or value["n"] < 0:
                raise ValueError("refusing to render a metric without its n")
            metric = value["value"]
            if metric is not None and (
                type(metric) not in {int, float}
                or not math.isfinite(metric)
                or value["n"] == 0
            ):
                raise ValueError("metric value must be finite and have a positive n")
        for child in value.values():
            validate_rates(child)
    elif isinstance(value, list):
        for child in value:
            validate_rates(child)


def metric_rows(value: dict[str, Any], prefix: str = "") -> list[str]:
    rows = []
    for key, child in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(child, dict) and "value" in child:
            number = "undefined" if child["value"] is None else f"{child['value']:.6f}"
            interval = child.get("ci95")
            bounds = (
                "undefined"
                if interval is None
                else f"[{interval[0]:.6f}, {interval[1]:.6f}]"
            )
            rows.append(
                f"| {name} | {number} | {child.get('successes', '—')} | {child['n']} | {bounds} |"
            )
        elif isinstance(child, dict):
            rows.extend(metric_rows(child, name))
    return rows


def render_markdown(report: dict[str, Any]) -> str:
    validate_rates(report["metrics"])
    lines = [
        "```json",
        json.dumps(report["config"], sort_keys=True, indent=2),
        "```",
        "",
        "# Evaluation summary",
        "",
        f"Total runs: {report['metrics']['n_runs']}.",
        "",
        "| Metric | Value | Success count | n | 95% interval |",
        "|---|---:|---:|---:|---|",
    ]
    lines += metric_rows(report["metrics"])
    lines += [
        "",
        "## Detector counts",
        "",
        "| Source | Kind | TP | FP | FN | TN | Labeled runs |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for source in ("injected", "manual"):
        for kind, counts in report["metrics"][source]["per_kind"].items():
            lines.append(
                f"| {source} | {kind} | {counts['tp']} | {counts['fp']} | {counts['fn']} | {counts.get('tn', '—')} | {counts.get('n_labeled', '—')} |"
            )
    paired = report["metrics"]["paired"]
    if paired:
        test = paired["mcnemar"]
        lines += [
            "",
            f"Exact McNemar p={test['p_value']:.6g}; n={test['n']} pairs, discordant n={test['discordant_n']}, v1-only={test['v1_only_success']}, v2-only={test['v2_only_success']}.",
            f"Bootstrap resamples={paired['success_difference']['resamples']}, seed={paired['success_difference']['seed']}.",
        ]
    lines += [
        "",
        f"Injection scoring n={report['metrics']['injected']['n_runs']} runs; unscored faults={report['metrics']['injected']['unscored_faults']}.",
        "",
        "## Interpretation",
        "",
    ]
    lines += ["- " + note for note in report["notes"]]
    return "\n".join(lines) + "\n"


def write_report(
    store: TraceStore, out: str | Path, evaluation_id: str | None = None
) -> dict[str, Any]:
    report = build_report(store, evaluation_id)
    markdown = render_markdown(report)
    encoded = (
        json.dumps(
            report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    )
    path = Path(out)
    path.mkdir(parents=True, exist_ok=True)
    (path / "summary.json").write_text(encoded, encoding="utf-8")
    (path / "summary.md").write_text(markdown, encoding="utf-8")
    return report
