"""Shared local tool contracts, validation, and stable state fingerprints."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, ClassVar

from ...canonical import canonical_json
from ...json_validation import schema_errors


class EnvironmentError(ValueError):
    """An invalid tool operation that an agent can recover from."""


def tool_schema(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


class Environment:
    schemas: ClassVar[list[dict[str, Any]]] = []

    def tools(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.schemas)

    def call(self, name: str, args: dict[str, Any]) -> Any:
        schema = next(
            (s["function"] for s in self.schemas if s["function"]["name"] == name), None
        )
        if schema is None:
            raise EnvironmentError(f"unknown tool {name!r}")
        try:
            encoded = json.dumps(args, allow_nan=False)
            saved = json.loads(encoded)
            if saved != args:
                raise ValueError("arguments must use JSON-native types")
        except (TypeError, ValueError, OverflowError) as error:
            raise EnvironmentError(
                "arguments must be finite JSON-native values"
            ) from error
        errors = schema_errors(args, schema["parameters"])
        if errors:
            raise EnvironmentError("invalid arguments: " + "; ".join(errors))
        return getattr(self, name)(**copy.deepcopy(args))

    def snapshot(self) -> dict[str, Any]:
        raise NotImplementedError

    def reset(self, seed: int) -> None:
        raise NotImplementedError

    def state_digest(self) -> str:
        return hashlib.sha256(canonical_json(self.snapshot()).encode()).hexdigest()
