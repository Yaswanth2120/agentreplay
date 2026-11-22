"""Isolated JSON key-value state; missing keys are explicit recoverable errors."""

from __future__ import annotations

import copy
from typing import Any, ClassVar

from .base import Environment, EnvironmentError, tool_schema

KEY = {"type": "string", "minLength": 1}


class KeyValue(Environment):
    schemas: ClassVar[list[dict[str, Any]]] = [
        tool_schema("get", "Read an existing key.", {"key": KEY}, ["key"]),
        tool_schema(
            "set",
            "Set or replace a JSON value.",
            {"key": KEY, "value": {}},
            ["key", "value"],
        ),
        tool_schema("delete", "Delete an existing key.", {"key": KEY}, ["key"]),
        tool_schema("list", "List keys in sorted order.", {}, []),
    ]

    def __init__(self, seed: int = 0) -> None:
        self.reset(seed)

    def reset(self, seed: int) -> None:
        self.values: dict[str, Any] = {"baseline": seed}

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.values)

    def get(self, key: str) -> Any:
        if key not in self.values:
            raise EnvironmentError(f"missing key {key!r}")
        return copy.deepcopy(self.values[key])

    def set(self, key: str, value: Any) -> Any:
        self.values[key] = copy.deepcopy(value)
        return self.get(key)

    def delete(self, key: str) -> bool:
        if key not in self.values:
            raise EnvironmentError(f"missing key {key!r}")
        del self.values[key]
        return True

    def list(self) -> list[str]:
        return sorted(self.values)
