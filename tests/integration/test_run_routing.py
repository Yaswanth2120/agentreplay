import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from conftest import chat_req, chat_resp
from fastapi.testclient import TestClient

from agentreplay.proxy import create_app
from agentreplay.schema import Exchange, Trace
from agentreplay.store import TraceStore


def seed_trace(store: TraceStore, name: str) -> str:
    trace = store.create_trace(Trace(name=name))
    for index in range(2):
        store.add_exchange(
            Exchange.from_request(
                trace.id,
                index,
                chat_req(str(index)),
                status_code=200,
                response=chat_resp(name),
            )
        )
    return trace.id


def test_concurrent_runs_same_and_different_traces_never_interfere(
    store: TraceStore,
) -> None:
    first, second = seed_trace(store, "first"), seed_trace(store, "second")
    app = create_app(store, "replay", trace_id=first)

    async def exercise() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client,
        ):
            responses = await asyncio.gather(
                *[
                    client.post("/_agentreplay/runs", json={"trace_id": trace_id})
                    for trace_id in [first, second, first]
                ]
            )
            ids = [response.json()["run_id"] for response in responses]
            barrier = asyncio.Event()

            async def consume(run_id: str, expected: str) -> None:
                await barrier.wait()
                for index in range(2):
                    reply = await client.post(
                        "/v1/chat/completions",
                        json=chat_req(str(index)),
                        headers={"X-AgentReplay-Run": run_id},
                    )
                    assert reply.status_code == 200
                    assert reply.json()["choices"][0]["message"]["content"] == expected
                    await asyncio.sleep(0)
                finished = await client.post(
                    f"/_agentreplay/runs/{run_id}/finish", json={}
                )
                assert finished.status_code == 200 and finished.json()["findings"] == []

            tasks = [
                asyncio.create_task(consume(run_id, expected))
                for run_id, expected in zip(
                    ids, ["first", "second", "first"], strict=True
                )
            ]
            barrier.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            # The startup run still has its own full consumption state.
            default = await client.post("/v1/chat/completions", json=chat_req("0"))
            assert default.status_code == 200 and app.state.replayer.remaining == 1

    asyncio.run(exercise())


def test_control_finish_persists_misses_leftovers_and_rejects_closed_runs(
    store: TraceStore,
) -> None:
    trace_id = seed_trace(store, "control")
    with TestClient(create_app(store, "replay", trace_id=trace_id)) as client:
        run_id = client.app.state.run_id
        wrong = client.post("/v1/chat/completions", json=chat_req("changed"))
        assert (
            wrong.status_code == 409 and wrong.json()["error"]["type"] == "replay_miss"
        )
        response = client.post(
            f"/_agentreplay/runs/{run_id}/finish",
            json={"outcome": "agent_error", "detail": "agent surfaced 409"},
        )
        findings = response.json()["findings"]
        assert [finding["kind"] for finding in findings] == [
            "replay_miss",
            "replay_leftover",
        ]
        assert findings[0]["evidence"]["nearest_exchange"]["seq"] == 0
        assert findings[0]["evidence"]["diff"]["changed"]
        assert response.json()["run"]["outcome"] == "agent_error"
        assert (
            client.post(f"/_agentreplay/runs/{run_id}/finish", json={}).status_code
            == 400
        )
        assert (
            client.post("/v1/chat/completions", json=chat_req("0")).status_code == 400
        )
        assert len(store.findings(run_id)) == 2


@pytest.mark.parametrize(
    "case", ["unknown_header", "missing_trace", "wrong_kind", "invalid_mode"]
)
def test_run_errors_are_rejected(store: TraceStore, case: str) -> None:
    trace_id = seed_trace(store, "errors")
    with TestClient(create_app(store, "replay", trace_id=trace_id)) as client:
        if case == "unknown_header":
            response = client.post(
                "/v1/chat/completions",
                json=chat_req(),
                headers={"X-AgentReplay-Run": "unknown"},
            )
        else:
            payload = (
                {"trace_id": "missing"}
                if case == "missing_trace"
                else (
                    {"kind": "record"}
                    if case == "wrong_kind"
                    else {"replay_mode": "invalid"}
                )
            )
            response = client.post("/_agentreplay/runs", json=payload)
        assert response.status_code == (422 if case == "invalid_mode" else 400)
        assert client.app.state.replayer.remaining == 2


def test_record_routing_and_busy_run_cannot_finish(store: TraceStore) -> None:
    first = store.create_trace(Trace(name="first"))
    second = store.create_trace(Trace(name="second"))

    async def exercise() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        class GatedStream(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                started.set()
                yield b'data: {"choices":[{"index":0,"delta":{"content":"hello"}}]}\n\n'
                await release.wait()
                yield b"data: [DONE]\n\n"

        app = create_app(
            store,
            "record",
            trace_id=first.id,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=GatedStream())
            ),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client,
        ):
            created = await client.post(
                "/_agentreplay/runs", json={"kind": "live", "trace_id": second.id}
            )
            run_id = created.json()["run_id"]
            pending = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    json=chat_req(stream=True),
                    headers={"X-AgentReplay-Run": run_id},
                )
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            busy = await client.post(f"/_agentreplay/runs/{run_id}/finish", json={})
            assert busy.status_code == 400 and "in flight" in busy.text
            assert store.get_run(run_id).finished_at is None
            release.set()
            assert (await asyncio.wait_for(pending, timeout=5)).status_code == 200
            assert (
                len(store.exchanges(second.id)) == 1 and store.exchanges(first.id) == []
            )
            finished = await client.post(
                f"/_agentreplay/runs/{run_id}/finish", json={"task_success": True}
            )
            assert (
                finished.status_code == 200
                and finished.json()["run"]["task_success"] is True
            )

    asyncio.run(exercise())


def test_run_context_shares_manager_consumption_and_finalizes(
    store: TraceStore,
) -> None:
    from agentreplay.runs import RunRecorder
    from agentreplay.schema import Run

    trace_id = seed_trace(store, "context")
    with TestClient(create_app(store, "replay", trace_id=trace_id)) as client:
        context = RunRecorder(
            store,
            Run(kind="replay", trace_id=trace_id),
            manager=client.app.state.runs,
            clock=lambda: 9.0,
        )
        with context as run:
            assert context.replay is client.app.state.runs.session(run.id)
            for index in range(2):
                reply = client.post(
                    "/v1/chat/completions",
                    json=chat_req(str(index)),
                    headers={"X-AgentReplay-Run": run.id},
                )
                assert reply.status_code == 200
        assert run.outcome == "completed" and store.findings(run.id) == []


@pytest.mark.parametrize("disconnect_on", ["body", "headers"])
def test_disconnect_closes_capture_before_releasing_run(
    store: TraceStore, disconnect_on: str
) -> None:
    import json
    from typing import Any

    from starlette.requests import ClientDisconnect

    class EndlessStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    upstream = EndlessStream()
    app = create_app(
        store,
        "record",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=upstream)
        ),
    )

    async def exercise() -> None:
        async def receive() -> dict[str, Any]:
            return {
                "type": "http.request",
                "body": json.dumps(chat_req(stream=True)).encode(),
            }

        async def send(message: dict[str, Any]) -> None:
            target = (
                "http.response.body"
                if disconnect_on == "body"
                else "http.response.start"
            )
            if message["type"] == target:
                raise OSError("client disconnected")

        async with app.router.lifespan_context(app):
            with pytest.raises(ClientDisconnect):
                await app(
                    {
                        "type": "http",
                        "asgi": {"version": "3.0", "spec_version": "2.4"},
                        "http_version": "1.1",
                        "method": "POST",
                        "scheme": "http",
                        "path": "/v1/chat/completions",
                        "raw_path": b"/v1/chat/completions",
                        "query_string": b"",
                        "headers": [(b"content-type", b"application/json")],
                        "client": ("test", 1),
                        "server": ("test", 80),
                    },
                    receive,
                    send,
                )
            assert upstream.closed
            if disconnect_on == "body":
                assert len(store.exchanges(app.state.trace_id)[0].chunks) == 1
            else:
                assert store.exchanges(app.state.trace_id) == []
            assert (
                app.state.runs.finish(app.state.run_id, "aborted").outcome == "aborted"
            )

    asyncio.run(exercise())
