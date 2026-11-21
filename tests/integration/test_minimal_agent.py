import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import chat_resp

from agentreplay.proxy import create_app
from agentreplay.store import TraceStore
from agentreplay.tools import ToolRecorder
from examples.minimal_agent import add, run_agent


def test_example_records_and_replays_offline_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "agent.db"
    upstream_calls: list[dict[str, Any]] = []
    tool_calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        upstream_calls.append(body)
        if len(upstream_calls) == 1:
            return httpx.Response(
                200, json=chat_resp(None, tool=("add", '{"a":2,"b":3}'))
            )
        assert body["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "5",
        }
        return httpx.Response(200, json=chat_resp("2 + 3 = 5"))

    def counted_add(a: int, b: int) -> int:
        nonlocal tool_calls
        tool_calls += 1
        return add(a, b)

    def forbidden_upstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("replay called upstream")

    async def exercise() -> None:
        store = TraceStore(path)
        record_app = create_app(
            store, "record", transport=httpx.MockTransport(upstream)
        )
        trace_id = record_app.state.trace_id
        async with (
            record_app.router.lifespan_context(record_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=record_app),
                base_url="http://proxy",
            ) as client,
        ):
            recorded = await run_agent(
                client, ToolRecorder(store, trace_id, "record"), counted_add
            )
        assert len(upstream_calls) == 2 and tool_calls == 1
        assert len(store.exchanges(trace_id)) == 2
        assert len(store.tool_events(trace_id)) == 1
        store.close()

        reopened = TraceStore(path)
        replay_app = create_app(
            reopened,
            "replay",
            trace_id=trace_id,
            transport=httpx.MockTransport(forbidden_upstream),
        )
        async with (
            replay_app.router.lifespan_context(replay_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=replay_app),
                base_url="http://proxy",
            ) as client,
        ):
            replayed = await run_agent(
                client, ToolRecorder(reopened, trace_id, "replay"), counted_add
            )
        assert replayed == recorded == "2 + 3 = 5"
        assert len(upstream_calls) == 2 and tool_calls == 1
        assert replay_app.state.replayer.remaining == 0
        assert reopened.findings(replay_app.state.run_id) == []
        assert (
            len(reopened.exchanges(trace_id)) == 2
            and len(reopened.tool_events(trace_id)) == 1
        )
        reopened.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "case", ["http_error", "unknown_tool", "step_limit", "direct_answer"]
)
def test_example_surfaces_errors_and_limits_steps(case: str, tmp_path: Path) -> None:
    def scripted(request: httpx.Request) -> httpx.Response:
        if case == "http_error":
            return httpx.Response(429, json={"error": "limited"})
        if case == "direct_answer":
            return httpx.Response(200, json=chat_resp("5"))
        name = "unknown" if case == "unknown_tool" else "add"
        return httpx.Response(200, json=chat_resp(None, tool=(name, '{"a":2,"b":3}')))

    async def exercise() -> None:
        store = TraceStore(tmp_path / "example.db")
        app = create_app(store, "record", transport=httpx.MockTransport(scripted))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://proxy",
            ) as client,
        ):
            recorder = ToolRecorder(store, app.state.trace_id, "record")
            if case == "direct_answer":
                assert await run_agent(client, recorder) == "5"
            elif case == "http_error":
                with pytest.raises(httpx.HTTPStatusError):
                    await run_agent(client, recorder)
            else:
                message = (
                    "unknown tool" if case == "unknown_tool" else "three-request limit"
                )
                with pytest.raises(ValueError, match=message):
                    await run_agent(client, recorder)
            expected = 3 if case == "step_limit" else 0
            assert len(store.tool_events(app.state.trace_id)) == expected
        store.close()

    asyncio.run(exercise())
