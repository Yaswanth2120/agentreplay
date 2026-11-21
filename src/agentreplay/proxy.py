"""OpenAI-compatible recording/replay HTTP surface and SSE passthrough."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .replay import Replayer, ReplayMiss
from .schema import Exchange, Finding, ReplayMode, Run, Trace
from .store import TraceStore
from .streaming import emit_chunks, reassemble, synthesize_chunks


def create_app(
    store: TraceStore,
    mode: Literal["record", "replay"],
    trace_id: str | None = None,
    trace_name: str = "unnamed",
    upstream_url: str = "http://localhost:8000",
    replay_mode: ReplayMode = "strict",
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 300.0,
    clock: Callable[[], float] = time.perf_counter,
    wall_clock: Callable[[], float] = time.time,
) -> FastAPI:
    client = None
    if mode == "record":
        trace = store.get_trace(trace_id) if trace_id else None
        trace = trace or store.create_trace(
            Trace(id=trace_id, name=trace_name, created_at=wall_clock())
            if trace_id
            else Trace(name=trace_name, created_at=wall_clock())
        )
        trace_id = trace.id
        client = httpx.AsyncClient(
            base_url=upstream_url, transport=transport, timeout=timeout
        )
    elif not trace_id or not store.get_trace(trace_id):
        raise ValueError("replay mode requires an existing trace_id")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if client is not None:
                await client.aclose()

    app = FastAPI(title="agentreplay-proxy", lifespan=lifespan)
    app.state.trace_id = trace_id
    run = store.create_run(
        Run(
            kind=mode,
            trace_id=trace_id,
            replay_mode=replay_mode if mode == "replay" else None,
            started_at=wall_clock(),
        )
    )
    app.state.run_id = run.id
    if mode == "replay":
        app.state.replayer = Replayer(store.exchanges(trace_id), replay_mode)

    def save(
        body: dict[str, Any],
        seq: int,
        started: float,
        status: int,
        response: dict[str, Any] | None,
        error: str | None,
        chunks: list[str] | None = None,
    ) -> None:
        store.add_exchange(
            Exchange.from_request(
                trace_id,
                seq,
                body,
                status_code=status,
                response=response,
                error=error,
                stream=bool(body.get("stream")),
                chunks=chunks,
                latency_ms=(clock() - started) * 1000,
                created_at=wall_clock(),
            )
        )

    def replay_response(body: dict[str, Any]) -> JSONResponse | StreamingResponse:
        try:
            ex = app.state.replayer.next(body)
        except ReplayMiss as error:
            return JSONResponse(
                {"error": {"type": "replay_miss", "message": str(error)}},
                status_code=409,
            )
        wants_stream = bool(body.get("stream"))
        if wants_stream != ex.stream:
            store.add_finding(
                Finding(
                    run_id=run.id,
                    kind="stream_mismatch",
                    severity="warning",
                    exchange_seq=ex.seq,
                    evidence={
                        "recorded_stream": ex.stream,
                        "requested_stream": wants_stream,
                    },
                    created_at=wall_clock(),
                )
            )
        if wants_stream and (
            ex.chunks is not None or (ex.response is not None and ex.status_code < 400)
        ):
            chunks = (
                ex.chunks if ex.chunks is not None else synthesize_chunks(ex.response)
            )
            return StreamingResponse(
                emit_chunks(chunks),
                status_code=ex.status_code,
                media_type="text/event-stream",
            )
        if ex.response is None:
            return JSONResponse(
                {"error": {"message": ex.error or "recorded error"}},
                status_code=ex.status_code,
            )
        return JSONResponse(ex.response, status_code=ex.status_code)

    async def capture_stream(
        upstream: httpx.Response,
        body: dict[str, Any],
        seq: int,
        started: float,
    ) -> AsyncIterator[str]:
        chunks: list[str] = []
        status, error = upstream.status_code, None
        try:
            async for line in upstream.aiter_lines():
                if line.startswith("data:"):
                    chunks.append(line)
                yield line + "\n"
        except httpx.HTTPError as exc:
            status, error = 599, f"{type(exc).__name__}: {exc}"
        finally:
            try:
                response = reassemble(chunks)
            except (ValueError, KeyError, TypeError) as exc:
                response = None
                error = error or f"{type(exc).__name__}: {exc}"
            save(body, seq, started, status, response, error, chunks)
            await upstream.aclose()

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(request: Request) -> JSONResponse | StreamingResponse:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(
                {"error": {"message": "body must be JSON"}}, status_code=400
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": {"message": "body must be a JSON object"}}, status_code=400
            )
        if mode == "replay":
            return replay_response(body)

        seq, started = store.next_seq(trace_id), clock()
        response, error = None, None
        try:
            upstream = await client.send(
                client.build_request("POST", "/v1/chat/completions", json=body),
                stream=True,
            )
            if body.get("stream") and upstream.status_code < 400:
                return StreamingResponse(
                    capture_stream(upstream, body, seq, started),
                    status_code=upstream.status_code,
                    media_type="text/event-stream",
                )
            try:
                await upstream.aread()
                status = upstream.status_code
                try:
                    response = upstream.json()
                except ValueError:
                    error = upstream.text[:2000]
            finally:
                await upstream.aclose()
        except httpx.HTTPError as exc:
            status, error = 599, f"{type(exc).__name__}: {exc}"
        save(body, seq, started, status, response, error)
        if response is None:
            return JSONResponse(
                {"error": {"message": error or "upstream error"}},
                status_code=502 if status == 599 else status,
            )
        return JSONResponse(response, status_code=status)

    return app
