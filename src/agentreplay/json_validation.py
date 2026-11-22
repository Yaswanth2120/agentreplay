"""Offline JSON parsing and schema validation shared by pure detectors."""

from __future__ import annotations

import json
from typing import Any

import jsonschema


def reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def parse_arguments(raw: str) -> dict[str, Any]:
    value = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise TypeError("tool arguments must be a JSON object")
    return value


def schema_errors(value: Any, schema: dict[str, Any]) -> list[str]:
    pending: list[Any] = [schema]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    key in {"$ref", "$dynamicRef"}
                    and isinstance(child, str)
                    and not child.startswith("#")
                ):
                    return ["external schema references are unsupported offline"]
                pending.append(child)
        elif isinstance(item, list):
            pending.extend(item)
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        validator = jsonschema.Draft202012Validator(schema)
        return sorted(
            f"{error.json_path}: {error.message}"[:200]
            for error in validator.iter_errors(value)
        )
    except (
        jsonschema.SchemaError,
        jsonschema.exceptions._RefResolutionError,
        TypeError,
        ValueError,
        RecursionError,
    ) as error:
        return [f"invalid or unresolved tool schema: {error}"[:200]]
