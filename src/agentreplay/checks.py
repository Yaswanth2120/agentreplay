"""Offline golden checks using the same proxy/session as application replay."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .exportio import import_trace
from .proxy import create_app
from .replay import ReplayMiss, ToolReplayMiss
from .schema import ReplayMode
from .store import TraceStore
from .tools import ToolRecorder, ToolReplayedError

DIVERGENCES = {"replay_miss", "tool_replay_miss", "replay_leftover", "replay_order"}


def load_agent(reference: str) -> Callable[..., Any]:
    module, separator, name = reference.partition(":")
    if not separator or not module or not name:
        raise ValueError("agent must be MODULE:CALLABLE")
    try:
        agent = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as error:
        raise ValueError(f"cannot load agent {reference!r}: {error}") from error
    if not callable(agent):
        raise TypeError(f"agent {reference!r} is not callable")
    try:
        inspect.signature(agent).bind(None, None)
    except (TypeError, ValueError) as error:
        raise ValueError("agent must accept (httpx_client, tool_recorder)") from error
    return agent


async def check_trace(
    path: Path, agent: Callable[..., Any], mode: ReplayMode
) -> dict[str, Any]:
    store = TraceStore()
    try:
        trace = import_trace(store, path)
        app = create_app(store, "replay", trace_id=trace.id, replay_mode=mode)
        session = app.state.session
        detail = None
        completed = False
        try:
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://agentreplay",
                    headers={"X-AgentReplay-Run": session.run.id},
                ) as client,
            ):
                result = agent(
                    client, ToolRecorder(store, trace.id, "replay", session=session)
                )
                if inspect.isawaitable(result):
                    await result
                completed = True
        except (
            httpx.HTTPError,
            ReplayMiss,
            ToolReplayMiss,
            ToolReplayedError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            RuntimeError,
            AssertionError,
        ) as error:
            detail = f"{type(error).__name__}: {error}"
        finally:
            # Unexpected exceptions still finalize and propagate as a failed process.
            pending = sys.exc_info()[1]
            if not completed and detail is None and pending is not None:
                detail = f"{type(pending).__name__}: {pending}"
            session.finish("completed" if completed else "agent_error", detail=detail)
        findings = store.findings(session.run.id)
        passed = completed and not any(
            finding.kind in DIVERGENCES or finding.severity == "error"
            for finding in findings
        )
        return {
            "file": path.name,
            "trace_id": trace.id,
            "run_id": session.run.id,
            "passed": passed,
            "outcome": session.run.outcome,
            "detail": detail,
            "findings": [finding.model_dump() for finding in findings],
        }
    finally:
        store.close()


def check_golden(
    directory: str | Path, reference: str, mode: ReplayMode = "strict"
) -> dict[str, Any]:
    if mode not in {"strict", "lenient"}:
        raise ValueError("check mode must be strict or lenient")
    directory = Path(directory)
    paths = sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
    if not paths:
        raise ValueError(f"no golden JSONL traces in {directory}")
    original_path = sys.path[:]
    try:
        # Match `python -m` so repository-local agent modules work from the installed CLI.
        sys.path.insert(0, str(Path.cwd()))
        agent = load_agent(reference)
        results = [asyncio.run(check_trace(path, agent, mode)) for path in paths]
        return {
            "passed": all(result["passed"] for result in results),
            "traces": results,
        }
    finally:
        sys.path[:] = original_path
