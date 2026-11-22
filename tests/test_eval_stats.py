"""Small hand-computed statistical examples; no external numeric dependencies."""

from __future__ import annotations

import copy
import random
from typing import Any

import pytest

from agentreplay.evals.stats import (
    bootstrap_paired,
    expected_kind,
    finding_signature,
    harmonic,
    injection_scores,
    manual_scores,
    mcnemar_exact,
    paired_values,
    percentile,
    precision_recall,
    rate,
    version_metrics,
    wilson,
)


def test_wilson_against_hand_computed_examples() -> None:
    # With z=1,n=1 the Wilson bounds are [0,.5] for k=0 and [.5,1] for k=1.
    assert wilson(0, 1, 1) == pytest.approx([0, 0.5])
    assert wilson(1, 1, 1) == pytest.approx([0.5, 1])
    # k=5,n=10,z=2: center=.5, radius=2*sqrt(.025+.01)/1.4=.2672612419.
    assert wilson(5, 10, 2) == pytest.approx([0.2327387581, 0.7672612419])
    assert wilson(0, 0) is None
    assert rate(0, 0) == {
        "value": None,
        "successes": 0,
        "n": 0,
        "ci95": None,
        "method": "Wilson",
    }
    assert rate(3, 4)["value"] == 0.75


@pytest.mark.parametrize(
    ("k", "n", "z"),
    [
        (-1, 2, 2),
        (3, 2, 2),
        (0, -1, 2),
        (True, 1, 2),
        (1, 1.0, 2),
        (0, 1, 0),
        (0, 1, float("nan")),
    ],
)
def test_invalid_wilson_inputs_rejected(k: int, n: int, z: float) -> None:
    with pytest.raises(ValueError):
        wilson(k, n, z)


def test_precision_recall_f1_counts_and_undefined_cases() -> None:
    # TP=3,FP=1,FN=2 -> P=3/4,R=3/5,F1=6/(6+1+2)=2/3.
    result = precision_recall(3, 1, 2)
    assert result["precision"]["value"] == 0.75 and result["precision"]["n"] == 4
    assert result["recall"]["value"] == 0.6 and result["recall"]["n"] == 5
    assert result["f1"]["value"] == pytest.approx(2 / 3)
    assert result["f1"]["n"] == 6
    low, high = result["f1"]["ci95"]
    assert 0 <= low < 2 / 3 < high <= 1
    assert harmonic(0, 0) == 0
    empty = precision_recall(0, 0, 0)
    assert all(empty[key]["value"] is None for key in ("precision", "recall", "f1"))
    assert precision_recall(0, 2, 3)["f1"]["value"] == 0
    assert precision_recall(0, 0, 3)["f1"]["ci95"] is None
    for args in [(-1, 0, 0), (0, 1.0, 0), (True, 0, 0)]:
        with pytest.raises(ValueError):
            precision_recall(*args)


def test_paired_bootstrap_seed_and_hand_computed_distribution() -> None:
    # Pair deltas [0,1]; two draws produce means 0,.5,1 with probabilities .25,.5,.25.
    # Therefore percentile 95% bounds are exactly [0,1], and the observed mean is .5.
    before = random.getstate()
    result = bootstrap_paired([False, False], [False, True])
    assert result["value"] == 0.5 and result["ci95"] == [0, 1] and result["n"] == 2
    assert result == bootstrap_paired([False, False], [False, True])
    assert random.getstate() == before
    # All deltas -1 -> every resample and both bounds are -1.
    assert bootstrap_paired([True, True], [False, False], resamples=10)["ci95"] == [
        -1,
        -1,
    ]
    assert percentile([0, 10], 0.25) == 2.5
    assert percentile([3], 1) == 3
    with pytest.raises(ValueError):
        percentile([], 0.5)
    with pytest.raises(ValueError):
        percentile([1], 2)
    with pytest.raises(ValueError):
        bootstrap_paired([True], [False], resamples=0)


def test_mcnemar_exact_against_binomial_hand_calculation() -> None:
    # Six discordant pairs all favor v1: two-sided p=2*C(6,0)/2**6=1/32.
    result = mcnemar_exact([True] * 6 + [True, False], [False] * 6 + [True, False])
    assert result["p_value"] == 1 / 32
    assert result["n"] == 8 and result["discordant_n"] == 6
    # b=3,c=1: p=2*(C(4,0)+C(4,1))/16=10/16=.625.
    assert (
        mcnemar_exact([True, True, True, False], [False, False, False, True])["p_value"]
        == 0.625
    )
    assert mcnemar_exact([True, False], [True, False])["p_value"] == 1
    assert mcnemar_exact([True, False], [False, True])["p_value"] == 1


@pytest.mark.parametrize(("a", "b"), [([], []), ([True], []), ([1], [False])])
def test_bad_pairs_rejected(a: list[bool], b: list[bool]) -> None:
    for function in (paired_values, mcnemar_exact, bootstrap_paired):
        with pytest.raises(ValueError):
            function(a, b)


def claim(
    kind: str, exchange: int | None = None, tool: int | None = None
) -> dict[str, Any]:
    return {"kind": kind, "exchange_seq": exchange, "tool_seq": tool}


def fault(
    kind: str, target: str = "exchange", seq: int = 0, **params: Any
) -> dict[str, Any]:
    return {"fault_class": kind, "target": target, "target_seq": seq, "params": params}


def test_injected_scores_match_kind_namespace_and_sequence_without_duplicates() -> None:
    rows = [
        {
            "faults": [
                fault("llm_http_error"),
                fault("tool_exec_error", "tool_event", 2),
                fault("tool_bad_result", "tool_event", 4),
            ],
            "findings": [
                claim("llm_error", 0),
                claim("llm_error", 0),
                claim("tool_error", tool=3),
                claim("tool_error", 2),
                claim("task_failure"),
            ],
        }
    ]
    result = injection_scores(rows)
    assert result["per_kind"]["llm_error"]["tp"] == 1
    assert result["per_kind"]["llm_error"]["fp"] == 0
    assert result["per_kind"]["tool_error"]["tp"] == 0
    assert result["per_kind"]["tool_error"]["fp"] == 2
    assert result["per_kind"]["tool_error"]["fn"] == 1
    assert result["unscored_faults"] == 1
    assert expected_kind(fault("llm_http_error", status=504)) == "timeout"
    assert expected_kind(fault("llm_http_error", status=599)) == "timeout"
    assert expected_kind(fault("tool_wrong_name")) == "invalid_tool_call"
    assert expected_kind(fault("tool_bad_result")) is None
    assert injection_scores([])["per_kind"]["timeout"]["precision"]["value"] is None


def test_one_prediction_does_not_cover_two_injected_labels() -> None:
    result = injection_scores(
        [
            {
                "faults": [fault("tool_loop"), fault("tool_loop", "tool_event")],
                "findings": [claim("loop", 0, 0)],
            }
        ]
    )
    assert result["per_kind"]["loop"]["tp"] == 1
    assert result["per_kind"]["loop"]["fn"] == 1


def test_manual_presence_scores_include_negatives_and_require_adjudication() -> None:
    rows = [
        {
            "labels": [
                {"kind": "loop", "present": True},
                {"kind": "timeout", "present": False},
            ],
            "findings": [claim("loop")],
        },
        {
            "labels": [
                {"kind": "loop", "present": True},
                {"kind": "timeout", "present": False},
            ],
            "findings": [claim("timeout")],
        },
    ]
    result = manual_scores(rows)["per_kind"]
    assert result["loop"]["tp"] == 1 and result["loop"]["fn"] == 1
    assert result["timeout"]["tn"] == 1 and result["timeout"]["fp"] == 1
    assert result["timeout"]["n_labeled"] == 2
    rows[0]["labels"].append({"kind": "loop", "present": False})
    with pytest.raises(ValueError, match="adjudication"):
        manual_scores(rows)


def test_version_metrics_counts_and_stable_finding_sets() -> None:
    rows = [
        {
            "run": {"task_success": True},
            "call_count": 4,
            "steps": 2,
            "findings": [claim("invalid_tool_call"), claim("loop")],
        },
        {
            "run": {"task_success": False},
            "call_count": 2,
            "steps": 4,
            "findings": [claim("timeout")],
        },
    ]
    result = version_metrics(rows)
    assert result["task_success"]["value"] == 0.5 and result["task_success"]["n"] == 2
    assert result["tool_call_validity"]["value"] == 5 / 6
    assert result["tool_call_validity"]["n"] == 6
    assert result["mean_steps"] == {"value": 3, "n": 2, "total": 6}
    rows[0]["call_count"] = rows[1]["call_count"] = 0
    with pytest.raises(ValueError):
        version_metrics(rows)
    first = {
        "run": {"outcome": "completed", "task_success": False},
        "result": {"run_id": "one", "final_state": {"x": 1}},
        "findings": [{**claim("loop", 1), "id": "a", "run_id": "one", "created_at": 1}],
    }
    second = copy.deepcopy(first)
    second["result"]["run_id"] = "two"
    second["findings"][0].update(id="b", run_id="two", created_at=2)
    assert finding_signature(first) == finding_signature(second)
    second["findings"][0]["exchange_seq"] = 2
    assert finding_signature(first) != finding_signature(second)
