"""Record synchronous Python tool calls or replay their JSON results offline."""

from __future__ import annotations

import inspect
import json
import sys
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, Literal

from .canonical import tool_args_hash
from .replay import ReplaySession, ToolReplayMiss
from .schema import Run, ToolEvent
from .store import TraceStore

__all__ = ["ToolRecorder", "ToolReplayMiss", "ToolReplayedError"]


class ToolReplayedError(Exception):
    def __init__(self, original_error: str) -> None:
        self.original_error = original_error
        super().__init__(original_error)


def _json_snapshot(value: Any, name: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as error:
        raise TypeError(
            f"tool {name!r} requires JSON-serializable arguments and results"
        ) from error


class ToolRecorder:
    def __init__(
        self,
        store: TraceStore,
        trace_id: str,
        mode: Literal["record", "replay"],
        faults: Callable[[ToolEvent, Any], Any] | None = None,
        *,
        clock: Callable[[], float] = time.perf_counter,
        wall_clock: Callable[[], float] = time.time,
        session: ReplaySession | None = None,
    ) -> None:
        if mode not in {"record", "replay"}:
            raise ValueError("tool mode must be 'record' or 'replay'")
        if store.get_trace(trace_id) is None:
            raise ValueError(f"unknown trace {trace_id!r}")
        self.store, self.trace_id, self.mode = store, trace_id, mode
        self.faults, self.clock, self.wall_clock = faults, clock, wall_clock
        if session is not None and (
            mode != "replay"
            or session.run.trace_id != trace_id
            or session.store is not store
        ):
            raise ValueError(
                "tool session must use the same store, trace, and replay mode"
            )
        self.session = session
        if mode == "replay" and self.session is None:
            run = store.create_run(
                Run(
                    kind="replay",
                    trace_id=trace_id,
                    replay_mode="strict",
                    started_at=wall_clock(),
                )
            )
            self.session = ReplaySession(store, run, clock=wall_clock)

    def call(self, name: str, args: dict[str, Any], fn: Callable[..., Any]) -> Any:
        if not isinstance(args, dict):
            raise TypeError(f"tool {name!r} arguments must be a dict")
        saved_args = _json_snapshot(args, name)
        if self.mode == "replay":
            event = self.session.next_tool(name, saved_args)
            if event.error is not None:
                raise ToolReplayedError(event.error)
            result = event.result
        else:
            event = ToolEvent(
                trace_id=self.trace_id,
                seq=self.store.next_tool_seq(self.trace_id),
                name=name,
                args=saved_args,
                args_hash=tool_args_hash(saved_args),
                created_at=self.wall_clock(),
            )
            started = self.clock()
            succeeded = False
            try:
                result = fn(**args)
                event.result = _json_snapshot(result, name)
                succeeded = True
            finally:
                # Preserve arbitrary tool exception types without catching Exception.
                error = None if succeeded else sys.exc_info()[1]
                if error is not None:
                    event.error = f"{type(error).__name__}: {error}"
                event.latency_ms = (self.clock() - started) * 1000
                self.store.add_tool_event(event)
        return self.faults(event, result) if self.faults is not None else result

    def tool(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            signature = inspect.signature(fn)
            unsupported = {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }
            if any(
                param.kind in unsupported for param in signature.parameters.values()
            ):
                raise TypeError(
                    f"tool {name!r} requires named, keyword-compatible parameters"
                )

            @wraps(fn)
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                return self.call(name, dict(bound.arguments), fn)

            return wrapped

        return decorate
