"""OpenAI-compatible recording/replay HTTP surface and SSE passthrough."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .replay import ReplayMiss
from .runs import RunManager
from .schema import CreateRunRequest, Exchange, FinishRunRequest, ReplayMode, Run, Trace
from .store import TraceStore
from .streaming import emit_chunks, reassemble, synthesize_chunks


class ClosingStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content: AsyncIterator[str],
        *,
        status_code: int = 200,
        media_type: str = "text/event-stream",
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(content, status_code=status_code, media_type=media_type)
        self.on_close = on_close

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Explicitly finalize capture and release the run even on downstream disconnect.
            try:
                await self.body_iterator.aclose()
            finally:
                if self.on_close is not None:
                    await self.on_close()


def create_app(
    store: TraceStore,
    mode: Literal["record", "live", "replay"],
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
    if mode != "replay":
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
    manager = RunManager(store, mode, clock=wall_clock)
    app.state.runs = manager
    if mode == "replay":
        app.state.session = manager.session(run.id)
        app.state.replayer = app.state.session.replayer

    def save(
        active: Run,
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
                active.trace_id,
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

    def replay_response(
        active: Run, body: dict[str, Any]
    ) -> JSONResponse | StreamingResponse:
        session = manager.session(active.id)
        try:
            ex = session.next_exchange(body)
        except ReplayMiss as error:
            return JSONResponse(
                {"error": {"type": "replay_miss", "message": str(error)}},
                status_code=409,
            )
        wants_stream = bool(body.get("stream"))
        if wants_stream != ex.stream:
            session.finding(
                "stream_mismatch",
                {
                    "recorded_stream": ex.stream,
                    "requested_stream": wants_stream,
                },
                severity="warning",
                exchange_seq=ex.seq,
            )
        if wants_stream and (
            ex.chunks is not None or (ex.response is not None and ex.status_code < 400)
        ):
            chunks = (
                ex.chunks if ex.chunks is not None else synthesize_chunks(ex.response)
            )
            return ClosingStreamingResponse(
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
        active: Run,
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
            save(active, body, seq, started, status, response, error, chunks)
            await upstream.aclose()

    async def record_response(
        active: Run, body: dict[str, Any]
    ) -> JSONResponse | StreamingResponse:
        seq, started = store.next_seq(active.trace_id), clock()
        response, error = None, None
        try:
            upstream = await client.send(
                client.build_request("POST", "/v1/chat/completions", json=body),
                stream=True,
            )
            if body.get("stream") and upstream.status_code < 400:
                return ClosingStreamingResponse(
                    capture_stream(active, upstream, body, seq, started),
                    status_code=upstream.status_code,
                    media_type="text/event-stream",
                    on_close=upstream.aclose,
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
        save(active, body, seq, started, status, response, error)
        if response is None:
            return JSONResponse(
                {"error": {"message": error or "upstream error"}},
                status_code=502 if status == 599 else status,
            )
        return JSONResponse(response, status_code=status)

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
        try:
            active = manager.begin(request.headers.get("X-AgentReplay-Run", run.id))
        except ValueError as error:
            return JSONResponse({"error": {"message": str(error)}}, status_code=400)
        streaming = False
        try:
            result = (
                replay_response(active, body)
                if active.kind == "replay"
                else await record_response(active, body)
            )
            if isinstance(result, ClosingStreamingResponse):
                close_upstream = result.on_close

                async def finish_stream() -> None:
                    try:
                        if close_upstream is not None:
                            await close_upstream()
                    finally:
                        manager.end(active.id)

                result.on_close = finish_stream
                streaming = True
            return result
        finally:
            if not streaming:
                manager.end(active.id)

    @app.post("/_agentreplay/runs")
    async def create_run(payload: CreateRunRequest) -> JSONResponse:
        values = payload.model_dump()
        values["trace_id"] = payload.trace_id or trace_id
        values["kind"] = payload.kind or mode
        values["replay_mode"] = (
            (payload.replay_mode or replay_mode) if values["kind"] == "replay" else None
        )
        if (values["kind"] == "replay") != (mode == "replay"):
            return JSONResponse(
                {"error": {"message": "run kind is incompatible with proxy mode"}},
                status_code=400,
            )
        if store.get_trace(values["trace_id"]) is None:
            return JSONResponse(
                {"error": {"message": "unknown trace"}}, status_code=400
            )
        created = store.create_run(Run(**values, started_at=wall_clock()))
        return JSONResponse({"run_id": created.id}, status_code=201)

    @app.post("/_agentreplay/runs/{run_id}/finish")
    async def finish_run(run_id: str, payload: FinishRunRequest) -> JSONResponse:
        try:
            finished = manager.finish(run_id, **payload.model_dump())
        except ValueError as error:
            return JSONResponse({"error": {"message": str(error)}}, status_code=400)
        return JSONResponse(
            {
                "run": finished.model_dump(),
                "findings": [
                    finding.model_dump() for finding in store.findings(run_id)
                ],
            }
        )

    return app
