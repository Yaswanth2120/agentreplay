"""Canonical JSON and semantic request hashing; no I/O."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# Fields that affect the model's output semantics. Everything else
# (stream, user, metadata, ...) is ignored when matching.
STRICT_KEYS = (
    "model",
    "messages",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "max_tokens",
    "seed",
    "response_format",
    "stop",
)
# Lenient mode ignores sampling/model knobs: match on conversation + tool defs only.
LENIENT_KEYS = ("messages", "tools")


def canonical_hash(request: dict[str, Any], keys: tuple[str, ...]) -> str:
    subset = {k: request[k] for k in keys if k in request}
    blob = canonical_json(subset)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def tool_args_hash(args: dict[str, Any] | str) -> str:
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except ValueError:
            parsed = None
        blob = canonical_json(parsed) if isinstance(parsed, dict) else "raw:" + args
    else:
        blob = canonical_json(args)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
