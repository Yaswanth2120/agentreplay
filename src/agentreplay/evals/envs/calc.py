"""Deterministic finite arithmetic and explicitly supported unit conversions."""

from __future__ import annotations

import math
from typing import Any, ClassVar

from .base import Environment, EnvironmentError, tool_schema

NUMBER = {"type": "number"}
UNITS = {
    "m": ("length", 1.0),
    "cm": ("length", 0.01),
    "km": ("length", 1000.0),
    "g": ("mass", 1.0),
    "kg": ("mass", 1000.0),
    "s": ("time", 1.0),
    "min": ("time", 60.0),
    "h": ("time", 3600.0),
}


class Calculator(Environment):
    schemas: ClassVar[list[dict[str, Any]]] = [
        tool_schema(
            "calculate",
            "Apply one finite arithmetic operation.",
            {
                "op": {"enum": ["add", "subtract", "multiply", "divide"]},
                "a": NUMBER,
                "b": NUMBER,
            },
            ["op", "a", "b"],
        ),
        tool_schema(
            "convert",
            "Convert compatible length, mass, or time units.",
            {
                "value": NUMBER,
                "source": {"enum": list(UNITS)},
                "target": {"enum": list(UNITS)},
            },
            ["value", "source", "target"],
        ),
    ]

    def __init__(self, seed: int = 0) -> None:
        self.reset(seed)

    def reset(self, seed: int) -> None:
        self.seed = seed

    def snapshot(self) -> dict[str, Any]:
        return {"seed": self.seed}

    def calculate(self, op: str, a: float, b: float) -> float:
        if not math.isfinite(a) or not math.isfinite(b):
            raise EnvironmentError("numbers must be finite")
        if op == "add":
            result = a + b
        elif op == "subtract":
            result = a - b
        elif op == "multiply":
            result = a * b
        elif op == "divide":
            if b == 0:
                raise EnvironmentError("division by zero")
            result = a / b
        else:
            raise EnvironmentError("unknown arithmetic operation")
        if not math.isfinite(result):
            raise EnvironmentError("arithmetic overflow")
        return result

    def convert(self, value: float, source: str, target: str) -> float:
        if (
            source not in UNITS
            or target not in UNITS
            or UNITS[source][0] != UNITS[target][0]
        ):
            raise EnvironmentError("incompatible or unknown units")
        result = value * UNITS[source][1] / UNITS[target][1]
        if not math.isfinite(result):
            raise EnvironmentError("conversion must be finite")
        return result
