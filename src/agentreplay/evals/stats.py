"""Pure count-based statistics; undefined rates retain n=0 and no numeric value."""

from __future__ import annotations

import math
import random
from typing import Any

from ..canonical import canonical_json

Z95 = 1.959963984540054
Z975 = 2.241402727604947


def wilson(successes: int, n: int, z: float = Z95) -> list[float] | None:
    if type(n) is not int or type(successes) is not int or not 0 <= successes <= n:
        raise ValueError("counts must be integers with 0 <= successes <= n")
    if not math.isfinite(z) or z <= 0:
        raise ValueError("z must be positive and finite")
    if n == 0:
        return None
    p = successes / n
    scale = 1 + z * z / n
    center = (p + z * z / (2 * n)) / scale
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / scale
    return [max(0.0, center - radius), min(1.0, center + radius)]


def rate(successes: int, n: int) -> dict[str, Any]:
    interval = wilson(successes, n)
    return {
        "value": successes / n if n else None,
        "successes": successes,
        "n": n,
        "ci95": interval,
        "method": "Wilson",
    }


def harmonic(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def precision_recall(tp: int, fp: int, fn: int) -> dict[str, Any]:
    if any(type(count) is not int or count < 0 for count in (tp, fp, fn)):
        raise ValueError("TP/FP/FN must be nonnegative integers")
    precision, recall = rate(tp, tp + fp), rate(tp, tp + fn)
    p_bounds, r_bounds = wilson(tp, tp + fp, Z975), wilson(tp, tp + fn, Z975)
    support = tp + fp + fn
    interval = (
        None
        if p_bounds is None or r_bounds is None
        else [harmonic(p_bounds[0], r_bounds[0]), harmonic(p_bounds[1], r_bounds[1])]
    )
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": {
            "value": 2 * tp / (2 * tp + fp + fn) if support else None,
            "n": support,
            "ci95": interval,
            "method": "conservative harmonic bounds from 97.5% Wilson precision/recall intervals",
        },
    }


def percentile(ordered: list[float], fraction: float) -> float:
    if not ordered or not 0 <= fraction <= 1:
        raise ValueError("percentile requires values and a fraction in [0,1]")
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def paired_values(a: list[bool], b: list[bool]) -> list[int]:
    if len(a) != len(b) or not a or any(type(value) is not bool for value in [*a, *b]):
        raise ValueError("paired samples must be nonempty equal-length boolean lists")
    return [int(right) - int(left) for left, right in zip(a, b, strict=True)]


def bootstrap_paired(
    a: list[bool], b: list[bool], *, resamples: int = 10000, seed: int = 1234
) -> dict[str, Any]:
    differences = paired_values(a, b)
    if type(resamples) is not int or resamples < 1:
        raise ValueError("resamples must be a positive integer")
    rng = random.Random(seed)
    n = len(differences)
    samples = sorted(
        sum(differences[rng.randrange(n)] for _ in range(n)) / n
        for _ in range(resamples)
    )
    return {
        "value": sum(differences) / n,
        "n": n,
        "ci95": [percentile(samples, 0.025), percentile(samples, 0.975)],
        "resamples": resamples,
        "seed": seed,
        "method": "paired percentile bootstrap; v2 minus v1",
    }


def mcnemar_exact(a: list[bool], b: list[bool]) -> dict[str, Any]:
    paired_values(a, b)
    b_count = sum(left and not right for left, right in zip(a, b, strict=True))
    c_count = sum(right and not left for left, right in zip(a, b, strict=True))
    discordant = b_count + c_count
    p = (
        min(
            1.0,
            2
            * sum(math.comb(discordant, i) for i in range(min(b_count, c_count) + 1))
            / 2**discordant,
        )
        if discordant
        else 1.0
    )
    return {
        "p_value": p,
        "n": len(a),
        "discordant_n": discordant,
        "v1_only_success": b_count,
        "v2_only_success": c_count,
        "method": "two-sided exact McNemar",
    }


DETECTOR_KINDS = (
    "invalid_tool_call",
    "loop",
    "timeout",
    "malformed_response",
    "llm_error",
    "tool_error",
    "budget_exceeded",
)


def expected_kind(fault: dict[str, Any]) -> str | None:
    if fault["fault_class"] == "llm_http_error":
        return (
            "timeout"
            if fault["params"].get("status", 500) in {504, 599}
            else "llm_error"
        )
    return {
        "llm_timeout": "timeout",
        "llm_malformed_json": "malformed_response",
        "llm_empty_response": "malformed_response",
        "tool_args_corrupt": "invalid_tool_call",
        "tool_wrong_name": "invalid_tool_call",
        "tool_args_schema_violation": "invalid_tool_call",
        "tool_loop": "loop",
        "tool_exec_timeout": "tool_error",
        "tool_exec_error": "tool_error",
    }.get(fault["fault_class"])


def injection_scores(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {kind: [0, 0, 0] for kind in DETECTOR_KINDS}
    unlocalized = 0
    for record in records:
        labels = set()
        for fault in record["faults"]:
            kind = expected_kind(fault)
            if kind is None:
                unlocalized += 1
            else:
                labels.add((kind, fault["target"], fault["target_seq"]))
        for kind in DETECTOR_KINDS:
            expected = sorted(label for label in labels if label[0] == kind)
            predictions = sorted(
                {
                    (finding.get("exchange_seq"), finding.get("tool_seq"))
                    for finding in record["findings"]
                    if finding["kind"] == kind
                },
                key=str,
            )
            remaining = list(predictions)
            tp = 0
            for _, target, seq in expected:
                match = next(
                    (
                        p
                        for p in remaining
                        if p[0 if target == "exchange" else 1] == seq
                    ),
                    None,
                )
                if match is not None:
                    tp += 1
                    remaining.remove(match)
            counts[kind][0] += tp
            counts[kind][1] += len(predictions) - tp
            counts[kind][2] += len(expected) - tp
    return {
        "per_kind": {
            kind: precision_recall(*values) for kind, values in counts.items()
        },
        "n_runs": len(records),
        "unscored_faults": unlocalized,
        "unit": "unique kind/target/sequence claims per run, one-to-one matching; task_failure is run-level and not localized",
    }


def manual_scores(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {kind: [0, 0, 0, 0] for kind in ("loop", "invalid_tool_call", "timeout")}
    for record in records:
        grouped: dict[str, set[bool]] = {}
        for label in record["labels"]:
            grouped.setdefault(label["kind"], set()).add(label["present"])
        for kind, values in grouped.items():
            if len(values) != 1:
                raise ValueError("conflicting manual labels require adjudication")
            actual = next(iter(values))
            predicted = any(f["kind"] == kind for f in record["findings"])
            index = (
                0 if actual and predicted else 1 if predicted else 2 if actual else 3
            )
            counts[kind][index] += 1
    return {
        "per_kind": {
            kind: {
                **precision_recall(*values[:3]),
                "tn": values[3],
                "n_labeled": sum(values),
            }
            for kind, values in counts.items()
        },
        "unit": "one presence label per run/kind; sequence annotations are informational; separate from injection scoring",
    }


def version_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    calls = sum(record["call_count"] for record in records)
    invalid = sum(
        sum(f["kind"] == "invalid_tool_call" for f in record["findings"])
        for record in records
    )
    if invalid > calls:
        raise ValueError("invalid call count exceeds observed calls")
    steps = sum(record["steps"] for record in records)
    n = len(records)
    return {
        "task_success": rate(
            sum(record["run"]["task_success"] is True for record in records), n
        ),
        "tool_call_validity": rate(calls - invalid, calls),
        "loop": rate(
            sum(any(f["kind"] == "loop" for f in r["findings"]) for r in records), n
        ),
        "timeout": rate(
            sum(any(f["kind"] == "timeout" for f in r["findings"]) for r in records), n
        ),
        "mean_steps": {"value": steps / n if n else None, "n": n, "total": steps},
    }


def finding_signature(record: dict[str, Any]) -> dict[str, Any]:
    findings = [
        {
            key: value
            for key, value in finding.items()
            if key not in {"id", "run_id", "created_at"}
        }
        for finding in record["findings"]
    ]
    return {
        "outcome": record["run"]["outcome"],
        "task_success": record["run"]["task_success"],
        "findings": sorted({canonical_json(finding) for finding in findings}),
        "result": {
            key: value for key, value in record["result"].items() if key != "run_id"
        },
    }


def summarize(records: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    sources = [r for r in records if r["run"]["config"]["phase"] == "record"]
    repeats = [r for r in records if r["run"]["config"]["phase"] == "clean_replay"]
    clean = [r for r in repeats if r["run"]["config"]["clean_source"]]
    regressions = [
        r for r in records if r["run"]["config"]["phase"] == "regression_replay"
    ]
    faults = [r for r in records if r["run"]["config"]["phase"] == "fault_replay"]
    comparisons = []
    for source in sources:
        pair = [
            r
            for r in repeats
            if r["run"]["config"]["source_run_id"] == source["run"]["id"]
        ]
        if len(pair) != 2:
            raise ValueError("determinism requires exactly two replay runs per source")
        comparisons.append(finding_signature(pair[0]) == finding_signature(pair[1]))
    kinds = sorted(
        set(DETECTOR_KINDS) | {f["kind"] for r in clean for f in r["findings"]}
    )
    versions = {
        version: version_metrics(
            [r for r in sources if r["run"]["agent_version"] == version]
        )
        for version in config["versions"]
    }
    paired = None
    if set(config["versions"]) == {"v1", "v2"}:
        indexed = {
            (r["run"]["task_id"], r["run"]["trial"], r["run"]["agent_version"]): r[
                "run"
            ]["task_success"]
            for r in sources
        }
        keys = sorted({(key[0], key[1]) for key in indexed})
        a = [indexed[(*key, "v1")] for key in keys]
        b = [indexed[(*key, "v2")] for key in keys]
        paired = {
            "success_difference": bootstrap_paired(
                a,
                b,
                resamples=config["bootstrap_resamples"],
                seed=config["bootstrap_seed"],
            ),
            "mcnemar": mcnemar_exact(a, b),
        }
    divergence = lambda record: any(
        f["kind"].startswith("replay_") or f["kind"] == "tool_replay_miss"
        for f in record["findings"]
    )
    return {
        "n_runs": len(records),
        "versions": versions,
        "replay_determinism": rate(sum(comparisons), len(comparisons)),
        "clean_false_positives": {
            "any": rate(sum(bool(r["findings"]) for r in clean), len(clean)),
            "per_kind": {
                kind: rate(
                    sum(any(f["kind"] == kind for f in r["findings"]) for r in clean),
                    len(clean),
                )
                for kind in kinds
            },
        },
        "regression_divergence": rate(
            sum(divergence(r) for r in regressions), len(regressions)
        ),
        "regression_kinds": {
            kind: rate(
                sum(any(f["kind"] == kind for f in r["findings"]) for r in regressions),
                len(regressions),
            )
            for kind in (
                "replay_miss",
                "tool_replay_miss",
                "replay_leftover",
                "replay_order",
            )
        },
        "injected": injection_scores(faults),
        "manual": manual_scores(records),
        "paired": paired,
    }
