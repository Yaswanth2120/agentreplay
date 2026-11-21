"""One-tool HTTPX agent; inject the client and recorder for offline replay."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from agentreplay.tools import ToolRecorder

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add",
            "description": "Add two integers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        },
    }
]


def add(a: int, b: int) -> int:
    return a + b


async def run_agent(
    client: httpx.AsyncClient,
    recorder: ToolRecorder,
    add_fn: Callable[..., int] = add,
) -> str:
    messages = [{"role": "user", "content": "Use add to calculate 2 + 3."}]
    for _ in range(3):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen3-8b",
                "messages": messages,
                "tools": TOOLS,
                "temperature": 0.0,
            },
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            return message["content"]
        for call in calls:
            function = call["function"]
            if function["name"] != "add":
                raise ValueError(f"unknown tool: {function['name']}")
            result = recorder.call("add", json.loads(function["arguments"]), add_fn)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result),
                }
            )
    raise ValueError("agent exceeded its three-request limit")
