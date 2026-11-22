"""Fake offline LLM: solves visible procedural instructions, never verifier goals."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..canonical import canonical_json


def response_message(
    content: str | None, calls: list[dict[str, Any]] | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        **({"tool_calls": calls} if calls else {}),
                    },
                    "finish_reason": "tool_calls" if calls else "stop",
                }
            ]
        },
    )


def tool_call(name: str, args: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "id": f"call-{index}",
        "type": "function",
        "function": {"name": name, "arguments": canonical_json(args)},
    }


def scripted_response(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    instruction = body["messages"][1]["content"]
    results = [
        json.loads(message["content"])
        for message in body["messages"]
        if message["role"] == "tool"
    ]
    if "\nStations: " in instruction:
        stations = json.loads(instruction.split("\nStations: ")[1])
        if len(results) < len(stations):
            return response_message(
                None,
                [tool_call("search", {"query": stations[len(results)]}, len(results))],
            )
        if any("error" in result for result in results):
            return response_message("Unable to read station evidence")
        codes = [
            result["result"][0]["text"].split("access code is ")[1].rstrip(".")
            for result in results
        ]
        return response_message(canonical_json({"codes": codes}))
    workflow = json.loads(instruction.split("\nWorkflow: ")[1])
    if isinstance(workflow, dict):
        if results and "error" in results[-1]:
            return response_message("Unable to complete calculation")
        value = results[-1]["result"] if results else workflow["start"]
        if len(results) == len(workflow["operations"]):
            return response_message(canonical_json({"result": value}))
        step = workflow["operations"][len(results)]
        args = (
            {"value": value, "source": step["source"], "target": step["target"]}
            if step["tool"] == "convert"
            else {"a": value, "op": step["op"], "b": step["b"]}
        )
        return response_message(None, [tool_call(step["tool"], args, len(results))])
    if not results:
        return response_message(
            None,
            [
                tool_call(step["tool"], step["args"], index)
                for index, step in enumerate(workflow)
            ],
        )
    return response_message("Done")
