"""SSE data-line parsing, completion reassembly, and immediate replay emission."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from .canonical import canonical_json


def parse_data_line(line: str) -> dict[str, Any] | None:
    if not line.startswith("data:"):
        return None
    payload = line[5:].lstrip(" ")
    if payload == "[DONE]":
        return None
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise TypeError("SSE data must be a JSON object")
    return value


def reassemble(chunks: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"object": "chat.completion", "choices": []}
    choices: dict[int, dict[str, Any]] = {}
    tools: dict[int, dict[int, dict[str, Any]]] = {}
    for line in chunks:
        chunk = parse_data_line(line)
        if chunk is None:
            continue
        for key in ("id", "created", "model", "system_fingerprint", "usage"):
            if key in chunk:
                result[key] = chunk[key]
        for part in chunk.get("choices", []):
            index = part.get("index", 0)
            choice = choices.setdefault(
                index,
                {
                    "index": index,
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": None,
                },
            )
            message = choice["message"]
            delta = part.get("delta") or {}
            if "role" in delta:
                message["role"] = delta["role"]
            if delta.get("content") is not None:
                message["content"] = (message["content"] or "") + delta["content"]
            for fragment in delta.get("tool_calls") or []:
                call = tools.setdefault(index, {}).setdefault(
                    fragment["index"],
                    {
                        "type": "function",
                        "function": {"arguments": ""},
                    },
                )
                if "id" in fragment and "id" not in call:
                    call["id"] = fragment["id"]
                function = fragment.get("function") or {}
                if "name" in function and "name" not in call["function"]:
                    call["function"]["name"] = function["name"]
                call["function"]["arguments"] += function.get("arguments") or ""
            if part.get("finish_reason") is not None:
                choice["finish_reason"] = part["finish_reason"]
            if "logprobs" in part:
                choice["logprobs"] = part["logprobs"]
    for index, choice in sorted(choices.items()):
        if index in tools:
            choice["message"]["tool_calls"] = [
                call for _, call in sorted(tools[index].items())
            ]
        result["choices"].append(choice)
    return result


def synthesize_chunks(response: dict[str, Any]) -> list[str]:
    chunk = {key: value for key, value in response.items() if key != "choices"}
    chunk["object"] = "chat.completion.chunk"
    chunk["choices"] = []
    for choice in response.get("choices", []):
        delta = dict(choice["message"])
        if delta.get("tool_calls"):
            delta["tool_calls"] = [
                {"index": i, **call} for i, call in enumerate(delta["tool_calls"])
            ]
        chunk["choices"].append(
            {
                "index": choice.get("index", 0),
                "delta": delta,
                "finish_reason": choice.get("finish_reason"),
            }
        )
    return ["data: " + canonical_json(chunk), "data: [DONE]"]


async def emit_chunks(chunks: list[str]) -> AsyncIterator[str]:
    for line in chunks:
        yield line + "\n\n"
    if not chunks or chunks[-1][5:].strip() != "[DONE]":
        yield "data: [DONE]\n\n"
