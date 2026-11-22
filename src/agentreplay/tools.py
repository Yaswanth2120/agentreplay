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
from .fault_runtime import FaultRuntime
from .faults import FaultPlan, FaultPlanError, plan_config
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
        faults: FaultPlan
        | FaultRuntime
        | Callable[[ToolEvent, Any], Any]
        | None = None,
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
        if isinstance(faults, FaultRuntime) and mode == "replay" and session is None:
            raise FaultPlanError("replay runtime requires its matching shared session")
        self.session = session
        plan = faults if isinstance(faults, FaultPlan) else None
        if (
            plan is not None
            and session is not None
            and session.run.fault_plan_id != plan.digest
        ):
            raise FaultPlanError("tool plan must match the shared session's plan")
        if session is None and (mode == "replay" or plan is not None):
            run = store.create_run(
                Run(
                    kind=mode,
                    trace_id=trace_id,
                    replay_mode="strict" if mode == "replay" else None,
                    started_at=wall_clock(),
                    fault_plan_id=plan.digest if plan is not None else None,
                    config=plan_config(plan),
                )
            )
            if mode == "replay":
                self.session = ReplaySession(store, run, clock=wall_clock)
            else:
                self.faults = FaultRuntime(store, run, clock=wall_clock)
        if mode == "replay" and (faults is None or plan is not None):
            self.faults = self.session.faults
        if isinstance(self.faults, FaultRuntime) and (
            self.faults.store is not store or self.faults.run.trace_id != trace_id
        ):
            raise FaultPlanError("tool runtime must use the same store and trace")
        if isinstance(self.faults, FaultRuntime) and (
            (mode == "record" and self.faults.run.kind == "replay")
            or (mode == "replay" and self.faults is not self.session.faults)
        ):
            raise FaultPlanError("tool runtime must belong to the active run/session")

    def call(self, name: str, args: dict[str, Any], fn: Callable[..., Any]) -> Any:
        if not isinstance(args, dict):
            raise TypeError(f"tool {name!r} arguments must be a dict")
        saved_args = _json_snapshot(args, name)
        if isinstance(self.faults, FaultRuntime):
            self.faults.ensure_open()
        if self.mode == "replay":
            event = self.session.next_tool(name, saved_args)
            if event.error is not None:
                self.session.faults.observed_tools.append(event.model_copy(deep=True))
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
                if error is not None and isinstance(self.faults, FaultRuntime):
                    self.faults.observed_tools.append(event.model_copy(deep=True))
        if isinstance(self.faults, FaultRuntime):
            return self._apply_faults(event, result)
        return self.faults(event, result) if self.faults is not None else result

    def _apply_faults(self, event: ToolEvent, result: Any) -> Any:
        observed = event.model_copy(deep=True)
        succeeded = False
        try:
            changed = self.faults(event, result)
            observed.result = _json_snapshot(changed, event.name)
            succeeded = True
            return changed
        finally:
            error = None if succeeded else sys.exc_info()[1]
            if error is not None:
                observed.result = None
                observed.error = f"{type(error).__name__}: {error}"
            self.faults.observed_tools.append(observed)
            if self.mode == "record":
                self.store.update_tool_event(observed)

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
