"""HTTPX reference tool agent; v2 deliberately skips one seeded batched call."""

from __future__ import annotations

import random
from functools import partial
from typing import Any, Literal

import httpx

from agentreplay.canonical import canonical_json
from agentreplay.evals.envs import Environment, EnvironmentError
from agentreplay.json_validation import parse_arguments
from agentreplay.schema import Task
from agentreplay.tools import ToolRecorder, ToolReplayedError

SYSTEM = (
    "Use the available tools to complete the task in order. Preserve unrelated state. "
    "Read tool errors and recover when possible. Never claim a tool action succeeded without its result. "
    "For answer tasks return only the requested JSON object, without markdown."
)


class AgentBudgetExceeded(ValueError):
    """The reference agent used its configured request budget."""


def execute_call(
    environment: Environment, call: dict[str, Any], recorder: ToolRecorder | None
) -> dict[str, Any]:
    function = call["function"]
    name = function["name"]
    try:
        args = parse_arguments(function["arguments"])
        if recorder is None:
            result = environment.call(name, args)
        else:
            result = recorder.call(
                name, args, partial(invoke_environment, environment, name)
            )
        return {"result": result}
    except (
        EnvironmentError,
        ToolReplayedError,
        ValueError,
        TypeError,
        KeyError,
        ArithmeticError,
        OSError,
    ) as error:
        return {"error": f"{type(error).__name__}: {error}"}


def invoke_environment(environment: Environment, name: str, **args: Any) -> Any:
    return environment.call(name, args)


async def run_agent(
    client: httpx.AsyncClient,
    task: Task,
    environment: Environment,
    *,
    model: str = "Qwen/Qwen3-8B",
    version: Literal["v1", "v2"] = "v1",
    trial: int = 0,
    regression_seed: int = 7,
    max_steps: int = 25,
    recorder: ToolRecorder | None = None,
    run_id: str | None = None,
    max_tokens: int | None = None,
    chat_template_kwargs: dict[str, Any] | None = None,
) -> str:
    if version not in {"v1", "v2"} or max_steps < 1 or trial < 0:
        raise ValueError("invalid agent version, request budget, or trial")
    rng = random.Random(regression_seed)
    skipped = False
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": task.instruction},
    ]
    for _ in range(max_steps):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "tools": environment.tools(),
                "temperature": 0.7,
                "seed": trial,
                **({"max_tokens": max_tokens} if max_tokens is not None else {}),
                **(
                    {"chat_template_kwargs": chat_template_kwargs}
                    if chat_template_kwargs
                    else {}
                ),
            },
            headers={"X-AgentReplay-Run": run_id} if run_id else {},
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        messages.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            if not isinstance(message.get("content"), str):
                raise ValueError("final assistant content must be text")
            return message["content"]
        # The documented v2 regression drops one execution from the first batch.
        drop = (
            rng.randrange(len(calls))
            if version == "v2" and not skipped and len(calls) > 1
            else None
        )
        for index, call in enumerate(calls):
            if index == drop:
                result = {"error": "v2 regression: skipped batched execution"}
                skipped = True
            else:
                result = execute_call(environment, call, recorder)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": canonical_json(result),
                }
            )
    raise AgentBudgetExceeded(f"agent exceeded {max_steps} requests")
