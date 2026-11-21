"""Generate deterministic golden artifacts with the real minimal agent and mocked LLM."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from agentreplay.exportio import export_trace
from agentreplay.proxy import create_app
from agentreplay.runs import RunRecorder
from agentreplay.schema import Run, Trace
from agentreplay.store import TraceStore
from agentreplay.tools import ToolRecorder
from examples.minimal_agent import run_agent

FIXTURE_TIME = 1_700_000_000.0


def mock_completion(request: httpx.Request, repeats: int) -> httpx.Response:
    body = json.loads(request.content)
    completed = sum(message["role"] == "tool" for message in body["messages"])
    message: dict[str, Any] = {"role": "assistant", "content": "2 + 3 = 5"}
    finish = "stop"
    if completed < repeats:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"add-{completed}",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a":2,"b":3}'},
                }
            ],
        }
        finish = "tool_calls"
    return httpx.Response(
        200,
        json={
            "id": f"mock-{completed}",
            "object": "chat.completion",
            "model": "qwen3-8b",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        },
    )


async def record_golden(path: Path, repeats: int) -> None:
    store, normalized = TraceStore(), TraceStore()
    try:
        trace = store.create_trace(
            Trace(
                id=path.stem,
                name=path.stem,
                created_at=FIXTURE_TIME,
                agent_name="minimal_agent",
                agent_version="v1",
                model="qwen3-8b",
                metadata={"fixture": "mock upstream", "tool_repeats": repeats},
            )
        )
        app = create_app(
            store,
            "record",
            trace_id=trace.id,
            transport=httpx.MockTransport(
                lambda request: mock_completion(request, repeats)
            ),
            clock=lambda: 0.0,
            wall_clock=lambda: FIXTURE_TIME,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://fixture",
            ) as client,
        ):
            with RunRecorder(
                store, Run(kind="record", trace_id=trace.id), clock=lambda: FIXTURE_TIME
            ) as run:
                answer = await run_agent(
                    client,
                    ToolRecorder(
                        store,
                        trace.id,
                        "record",
                        clock=lambda: 0.0,
                        wall_clock=lambda: FIXTURE_TIME,
                    ),
                )
                if answer != "2 + 3 = 5":
                    raise ValueError(f"unexpected fixture answer: {answer!r}")
                run.task_success = True
        # Normalize only random row IDs; all request/response/tool data came from execution.
        exchanges = [
            exchange.model_copy(update={"id": f"{trace.id}-exchange-{exchange.seq}"})
            for exchange in store.exchanges(trace.id)
        ]
        events = [
            event.model_copy(update={"id": f"{trace.id}-tool-{event.seq}"})
            for event in store.tool_events(trace.id)
        ]
        normalized.import_trace(trace, exchanges, events)
        export_trace(normalized, trace.id, path)
    finally:
        normalized.close()
        store.close()


def generate_golden(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = [directory / "minimal-add.jsonl", directory / "minimal-add-repeated.jsonl"]
    for path, repeats in zip(paths, [1, 2], strict=True):
        asyncio.run(record_golden(path, repeats))
    return paths


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("tests/golden"))
    args = parser.parse_args(argv)
    generate_golden(args.out)


if __name__ == "__main__":
    main()
