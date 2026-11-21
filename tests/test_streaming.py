import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import chat_req, chat_resp
from fastapi.testclient import TestClient

from agentreplay.canonical import (
    LENIENT_KEYS,
    STRICT_KEYS,
    canonical_hash,
    canonical_json,
)
from agentreplay.proxy import create_app
from agentreplay.schema import SCHEMA_VERSION, Exchange, Finding, Run, Trace
from agentreplay.store import SchemaVersionError, TraceStore
from agentreplay.streaming import (
    emit_chunks,
    parse_data_line,
    reassemble,
    synthesize_chunks,
)


def data(
    delta: dict[str, Any] | None = None,
    finish: str | None = None,
    index: int = 0,
    **extra: Any,
) -> str:
    return "data: " + json.dumps(
        {
            "id": "completion",
            "object": "chat.completion.chunk",
            "model": "test",
            "choices": [
                {"index": index, "delta": delta or {}, "finish_reason": finish}
            ],
            **extra,
        },
        ensure_ascii=False,
    )


class BytesStream(httpx.AsyncByteStream):
    def __init__(
        self, parts: list[bytes], error: httpx.HTTPError | None = None
    ) -> None:
        self.parts = parts
        self.error = error
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self.parts:
            yield part
        if self.error:
            raise self.error

    async def aclose(self) -> None:
        self.closed = True


def transport_for(
    stream: httpx.AsyncByteStream,
    calls: list[httpx.Request],
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream"}
        )

    return httpx.MockTransport(handler)


def test_stream_record_replay_identical_offline(
    store: TraceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunks = [
        data({"role": "assistant", "content": "hé"}),
        data({"content": "llo"}),
        data(finish="stop"),
        'data: {"choices":[],"usage":{"total_tokens":7}}',
        "data: [DONE]",
    ]
    wire = ("\r\n\r\n".join(chunks) + "\r\n\r\n").encode()
    # Split UTF-8 and SSE lines across arbitrary transport boundaries.
    stream = BytesStream([wire[i : i + 1] for i in range(len(wire))])
    calls = []
    with TestClient(
        create_app(store, "record", transport=transport_for(stream, calls))
    ) as rec:
        recorded = rec.post("/v1/chat/completions", json=chat_req(stream=True))
        tid = rec.app.state.trace_id
    ex = store.exchanges(tid)[0]
    assert ex.stream and ex.chunks == chunks and stream.closed
    assert ex.response["choices"][0]["message"] == {
        "role": "assistant",
        "content": "héllo",
    }
    assert ex.response["usage"] == {"total_tokens": 7}

    async def no_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("replay attempted network access")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    with TestClient(create_app(store, "replay", trace_id=tid)) as rep:
        replayed = rep.post("/v1/chat/completions", json=chat_req(stream=True))
        assert not store.findings(rep.app.state.run_id)
    assert replayed.status_code == 200
    assert replayed.headers["content-type"].startswith("text/event-stream")
    assert recorded.content == replayed.content
    assert len(calls) == 1


def test_interleaved_tool_fragments_multiple_choices() -> None:
    chunks = [
        data(
            {
                "tool_calls": [
                    {
                        "index": 1,
                        "id": "b",
                        "function": {"name": "second", "arguments": '{"b":'},
                    },
                    {
                        "index": 0,
                        "id": "a",
                        "function": {"name": "first", "arguments": '{"a":'},
                    },
                ]
            }
        ),
        data({"content": "other"}, index=1),
        data(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "ignored",
                        "function": {"name": "ignored", "arguments": "1}"},
                    },
                    {"index": 1, "function": {"arguments": "2}"}},
                ]
            },
            finish="tool_calls",
        ),
        data(finish="stop", index=1),
        "data: [DONE]",
    ]
    response = reassemble(chunks)
    ex = Exchange.from_request(
        "t",
        0,
        chat_req(),
        status_code=200,
        stream=True,
        chunks=chunks,
        response=response,
    )
    calls = ex.tool_calls()
    assert [(call.id, call.name, call.arguments) for call in calls] == [
        ("a", "first", '{"a":1}'),
        ("b", "second", '{"b":2}'),
    ]
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert response["choices"][0]["message"]["content"] is None
    assert response["choices"][1]["message"]["content"] == "other"
    assert response["choices"][1]["finish_reason"] == "stop"
    assert reassemble(synthesize_chunks(response)) == response


def test_midstream_error_saves_partial_and_injected_clock(store: TraceStore) -> None:
    line = data({"content": "partial"})
    stream = BytesStream([(line + "\n\n").encode()], httpx.ReadError("connection lost"))
    ticks = iter([10.0, 10.125])
    with TestClient(
        create_app(
            store,
            "record",
            transport=transport_for(stream, []),
            clock=lambda: next(ticks),
            wall_clock=lambda: 123.0,
        )
    ) as rec:
        reply = rec.post("/v1/chat/completions", json=chat_req(stream=True))
        ex = store.exchanges(rec.app.state.trace_id)[0]
    assert line in reply.text and reply.status_code == 200
    assert ex.status_code == 599 and ex.error == "ReadError: connection lost"
    assert ex.chunks == [line] and ex.latency_ms == 125.0 and ex.created_at == 123.0
    assert ex.response["choices"][0]["message"]["content"] == "partial"
    assert stream.closed


@pytest.mark.parametrize("record_stream", [False, True])
def test_stream_mismatch_both_directions_persisted(
    store: TraceStore, record_stream: bool
) -> None:
    response = chat_resp("answer", tool=("search", '{"q":"x"}'))
    chunks = synthesize_chunks(response) if record_stream else None
    tid = store.create_trace(Trace(name="mismatch")).id
    store.add_exchange(
        Exchange.from_request(
            tid,
            0,
            chat_req(stream=record_stream),
            status_code=200,
            response=response,
            stream=record_stream,
            chunks=chunks,
        )
    )
    with TestClient(create_app(store, "replay", trace_id=tid)) as rep:
        reply = rep.post(
            "/v1/chat/completions", json=chat_req(stream=not record_stream)
        )
        findings = store.findings(rep.app.state.run_id)
    assert reply.status_code == 200
    if record_stream:
        assert reply.json() == response
    else:
        lines = [line for line in reply.text.splitlines() if line.startswith("data:")]
        assert lines[-1] == "data: [DONE]" and len(lines) == 2
        assert reassemble(lines) == response
    assert len(findings) == 1
    assert findings[0].kind == "stream_mismatch" and findings[0].severity == "warning"
    assert findings[0].exchange_seq == 0
    assert findings[0].evidence == {
        "recorded_stream": record_stream,
        "requested_stream": not record_stream,
    }


@pytest.mark.parametrize("version", [None, "1", "2", "3", "999"])
def test_reject_old_and_future_database_without_modifying(
    tmp_path: Path, version: str | None
) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        if version is None:
            conn.execute("CREATE TABLE traces (id TEXT PRIMARY KEY)")
        else:
            conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT INTO schema_meta VALUES (?,?)", ("schema_version", version)
            )
    before = path.read_bytes()
    with pytest.raises(SchemaVersionError, match=f"expected v{SCHEMA_VERSION}"):
        TraceStore(path)
    assert path.read_bytes() == before


def test_database_reopen_and_pragmas(tmp_path: Path) -> None:
    path = tmp_path / "new.db"
    store = TraceStore(path)
    trace = store.create_trace(Trace(name="persist"))
    run = store.create_run(Run(kind="replay", trace_id=trace.id))
    finding = Finding(
        run_id=run.id,
        kind="stream_mismatch",
        severity="warning",
        evidence={"z": "é", "a": 0.0},
    )
    store.add_finding(finding)
    assert store.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.conn.execute("SELECT value FROM schema_meta").fetchone()[0] == str(
        SCHEMA_VERSION
    )
    store.close()
    reopened = TraceStore(path)
    assert reopened.get_trace(trace.id) == trace
    assert reopened.list_traces() == [trace]
    assert reopened.findings(run.id) == [finding]
    assert reopened.get_trace("missing") is None
    reopened.close()


def test_sequences_reserved_under_lock(store: TraceStore) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(lambda _: store.next_seq("t"), range(100)))
    assert sorted(sequences) == list(range(100))
    assert store.next_seq("other") == 0


def test_canonical_json_and_stream_excluded() -> None:
    assert canonical_json({"z": "é", "a": 0.0}) == '{"a":0.0,"z":"é"}'
    assert canonical_json(0) != canonical_json(0.0)
    for keys in (STRICT_KEYS, LENIENT_KEYS):
        assert canonical_hash(chat_req(), keys) == canonical_hash(
            chat_req(stream=True), keys
        )
        assert canonical_hash(chat_req(stream=False), keys) == canonical_hash(
            chat_req(stream=True), keys
        )


@pytest.mark.parametrize("payload", ["not json", "[]", "null"])
def test_invalid_request_400(store: TraceStore, payload: str) -> None:
    with TestClient(
        create_app(
            store,
            "record",
            transport=httpx.MockTransport(
                lambda request: pytest.fail("invalid body reached upstream")
            ),
        )
    ) as client:
        assert client.post("/v1/chat/completions", content=payload).status_code == 400
        assert store.exchanges(client.app.state.trace_id) == []


def test_parser_and_done_emission() -> None:
    assert parse_data_line(": comment") is None
    assert parse_data_line("data: [DONE]") is None
    assert parse_data_line('data:{"choices":[]}') == {"choices": []}
    with pytest.raises(TypeError):
        parse_data_line("data: []")
    with pytest.raises(ValueError):
        parse_data_line("data: broken")

    async def collect() -> list[str]:
        return [line async for line in emit_chunks([data({"content": "x"})])]

    assert asyncio.run(collect())[-1] == "data: [DONE]\n\n"


def test_stream_is_forwarded_before_upstream_finishes(store: TraceStore) -> None:
    forwarded = []
    first = data({"content": "first"}) + "\n\n"

    class GatedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield first.encode()
            assert any(first.encode().splitlines()[0] in body for body in forwarded)
            yield (data(finish="stop") + "\n\ndata: [DONE]\n\n").encode()

    app = create_app(store, "record", transport=transport_for(GatedStream(), []))

    async def exercise() -> None:
        async def receive() -> dict[str, Any]:
            return {
                "type": "http.request",
                "body": json.dumps(chat_req(stream=True)).encode(),
            }

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                forwarded.append(message.get("body", b""))

        async with app.router.lifespan_context(app):
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

    asyncio.run(exercise())
    assert store.exchanges(app.state.trace_id)[0].chunks[-1] == "data: [DONE]"


def test_concurrent_streams_keep_request_sequence(store: TraceStore) -> None:
    async def exercise() -> None:
        second_started = asyncio.Event()

        class OrderedStream(httpx.AsyncByteStream):
            def __init__(self, first: bool) -> None:
                self.first = first

            async def __aiter__(self) -> AsyncIterator[bytes]:
                if self.first:
                    await second_started.wait()
                else:
                    second_started.set()
                yield (
                    data({"content": "first" if self.first else "second"}) + "\n\n"
                ).encode()

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(
                200, stream=OrderedStream(body["messages"][0]["content"] == "first")
            )

        app = create_app(store, "record", transport=httpx.MockTransport(handler))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client,
        ):
            first = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    json=chat_req("first", stream=True),
                )
            )
            # The event loop starts the first request before creating the second.
            await asyncio.sleep(0)
            second = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    json=chat_req("second", stream=True),
                )
            )
            replies = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
        assert all(reply.status_code == 200 for reply in replies)
        exchanges = store.exchanges(app.state.trace_id)
        assert [ex.request["messages"][0]["content"] for ex in exchanges] == [
            "first",
            "second",
        ]
        assert [ex.seq for ex in exchanges] == [0, 1]

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["connect", "timeout", "http"])
def test_stream_request_upstream_failures(store: TraceStore, failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "connect":
            raise httpx.ConnectError("offline")
        if failure == "timeout":
            raise httpx.ReadTimeout("too slow")
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    with TestClient(
        create_app(store, "record", transport=httpx.MockTransport(handler))
    ) as client:
        reply = client.post("/v1/chat/completions", json=chat_req(stream=True))
        ex = store.exchanges(client.app.state.trace_id)[0]
    assert reply.status_code == (429 if failure == "http" else 502)
    assert ex.status_code == (429 if failure == "http" else 599)
    if failure == "http":
        assert ex.response == {"error": {"message": "rate limited"}}
        with TestClient(create_app(store, "replay", trace_id=ex.trace_id)) as replay:
            repeated = replay.post("/v1/chat/completions", json=chat_req(stream=True))
        assert repeated.status_code == 429 and repeated.json() == reply.json()
    else:
        assert ex.error and ex.response is None


def test_malformed_stream_is_preserved_with_error(store: TraceStore) -> None:
    stream = BytesStream([b"data: {broken\n\n"])
    with TestClient(
        create_app(store, "record", transport=transport_for(stream, []))
    ) as client:
        reply = client.post("/v1/chat/completions", json=chat_req(stream=True))
        ex = store.exchanges(client.app.state.trace_id)[0]
    assert reply.content == b"data: {broken\n\n"
    assert ex.chunks == ["data: {broken"] and ex.response is None
    assert "JSONDecodeError" in ex.error and stream.closed


def test_replay_missing_done_adds_marker(store: TraceStore) -> None:
    trace = store.create_trace(Trace(name="partial"))
    chunks = [data({"content": "partial"})]
    store.add_exchange(
        Exchange.from_request(
            trace.id,
            0,
            chat_req(stream=True),
            status_code=200,
            stream=True,
            chunks=chunks,
            response=reassemble(chunks),
        )
    )
    with TestClient(create_app(store, "replay", trace_id=trace.id)) as client:
        reply = client.post("/v1/chat/completions", json=chat_req(stream=True))
    assert reply.text == chunks[0] + "\n\ndata: [DONE]\n\n"


def test_foreign_keys_and_canonical_stored_bodies(store: TraceStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.add_exchange(
            Exchange.from_request("missing", 0, chat_req(), status_code=200)
        )
    trace = store.create_trace(Trace(name="json", metadata={"z": 0.0, "a": "é"}))
    store.add_exchange(
        Exchange.from_request(
            trace.id,
            0,
            chat_req(),
            status_code=200,
            stream=True,
            chunks=["data: [DONE]"],
            response={"z": 0.0, "a": "é"},
        )
    )
    row = store.conn.execute("SELECT request,response,chunks FROM exchanges").fetchone()
    assert row["request"] == canonical_json(chat_req())
    assert row["response"] == '{"a":"é","z":0.0}'
    assert row["chunks"] == '["data: [DONE]"]'
