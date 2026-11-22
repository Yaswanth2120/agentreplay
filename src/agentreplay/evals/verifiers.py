"""Independent final-state/answer checks; no tool replay, fault labels, or LLM judge."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from ..canonical import canonical_json
from ..json_validation import reject_constant
from ..schema import Task


def answer_object(answer: str) -> dict[str, Any] | None:
    try:
        value = json.loads(answer, parse_constant=reject_constant)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def verify(task: Task, state: dict[str, Any], final_answer: str) -> bool:
    params = task.verifier.params
    if set(params) != {"expected"}:
        raise ValueError("verifier requires exactly an expected value")
    expected = params["expected"]
    if task.verifier.name == "exact_state":
        if task.env not in {"kv", "files", "tickets"} or not isinstance(expected, dict):
            raise ValueError("invalid exact-state verifier contract")
        return canonical_json(state) == canonical_json(expected)
    answer = answer_object(final_answer)
    if task.verifier.name == "numeric_answer":
        if (
            task.env != "calc"
            or type(expected) not in {int, float}
            or not Decimal(str(expected)).is_finite()
        ):
            raise ValueError("invalid numeric verifier contract")
        value = answer.get("result") if answer else None
        return (
            type(value) in {int, float}
            and Decimal(str(value)).is_finite()
            and abs(Decimal(str(value)) - Decimal(str(expected))) <= Decimal("0.000001")
        )
    if (
        task.env != "search"
        or not isinstance(expected, list)
        or not all(isinstance(code, str) for code in expected)
    ):
        raise ValueError("invalid search verifier contract")
    return answer is not None and answer.get("codes") == expected
