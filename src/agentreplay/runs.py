"""Explicit run lifecycle for callers that execute an agent in process."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from types import TracebackType

from .detection import finish_run
from .fault_runtime import FaultRuntime
from .replay import ReplaySession
from .schema import (
    DetectorConfig,
    EvaluationResult,
    Finding,
    Run,
    RunObservations,
    RunOutcome,
)
from .store import TraceStore


class RunRecorder:
    def __init__(
        self,
        store: TraceStore,
        run: Run,
        *,
        clock: Callable[[], float] = time.time,
        manager: RunManager | None = None,
    ) -> None:
        if manager is not None and manager.store is not store:
            raise ValueError("run manager must use the same store")
        self.manager = manager
        self.store, self.run, self.clock = store, run.model_copy(deep=True), clock
        self._entered = False
        self.replay: ReplaySession | None = None

    def __enter__(self) -> Run:
        if self._entered:
            raise RuntimeError("RunRecorder cannot be reused")
        if self.run.finished_at is not None or self.run.outcome is not None:
            raise ValueError("RunRecorder requires an unfinished run")
        if self.manager is not None and (
            (self.run.kind == "replay") != (self.manager.mode == "replay")
        ):
            raise ValueError("run kind is incompatible with manager mode")
        DetectorConfig.model_validate(self.run.config.get("detectors", {}))
        self.run.started_at = self.clock()
        self.store.create_run(self.run)
        if self.run.kind == "replay":
            self.replay = (
                self.manager.session(self.run.id)
                if self.manager is not None
                else ReplaySession(self.store, self.run, clock=self.clock)
            )
        self._entered = True
        return self.run

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        outcome: RunOutcome = "completed"
        if exc_type is not None:
            outcome = (
                "aborted"
                if issubclass(exc_type, (KeyboardInterrupt, SystemExit, GeneratorExit))
                else "agent_error"
            )
        detail = (
            f"{exc_type.__name__}: {exc}" if exc_type is not None else self.run.detail
        )
        if self.manager is not None:
            finished = self.manager.finish(
                self.run.id, outcome, task_success=self.run.task_success, detail=detail
            )
        elif self.replay is not None:
            finished = self.replay.finish(
                outcome, task_success=self.run.task_success, detail=detail
            )
        else:
            finished = finish_run(
                self.store,
                self.run,
                outcome,
                finished_at=self.clock(),
                task_success=self.run.task_success,
                detail=detail,
                observations=RunObservations(
                    exchanges=self.store.exchanges(self.run.trace_id),
                    tool_events=self.store.tool_events(self.run.trace_id),
                ),
            )
        self.run.finished_at = finished.finished_at
        self.run.outcome = finished.outcome
        self.run.detail = finished.detail


class RunManager:
    def __init__(
        self, store: TraceStore, mode: str, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.store, self.mode, self.clock = store, mode, clock
        self._sessions: dict[str, ReplaySession] = {}
        self._faults: dict[str, FaultRuntime] = {}
        self._active: dict[str, int] = {}
        self._lock = threading.RLock()

    def get_run(self, run_id: str) -> Run:
        run = self.store.get_run(run_id)
        if run is None or run.finished_at is not None:
            raise ValueError(f"run {run_id!r} is unknown or already finished")
        if (run.kind == "replay") != (self.mode == "replay"):
            raise ValueError("run kind is incompatible with proxy mode")
        return run

    def session(self, run_id: str) -> ReplaySession:
        with self._lock:
            run = self.get_run(run_id)
            if run_id not in self._sessions:
                self._sessions[run_id] = ReplaySession(
                    self.store, run, clock=self.clock
                )
            return self._sessions[run_id]

    def faults(self, run_id: str) -> FaultRuntime:
        with self._lock:
            run = self.get_run(run_id)
            if run.kind == "replay":
                return self.session(run_id).faults
            if run_id not in self._faults:
                self._faults[run_id] = FaultRuntime(self.store, run, clock=self.clock)
            return self._faults[run_id]

    def begin(self, run_id: str) -> Run:
        with self._lock:
            run = self.get_run(run_id)
            self._active[run_id] = self._active.get(run_id, 0) + 1
            return run

    def end(self, run_id: str) -> None:
        with self._lock:
            self._active[run_id] -= 1

    def finish(
        self,
        run_id: str,
        outcome: RunOutcome,
        *,
        task_success: bool | None = None,
        detail: str | None = None,
        findings: list[Finding] | None = None,
        evaluation: EvaluationResult | None = None,
    ) -> Run:
        with self._lock:
            run = self.get_run(run_id)
            if self._active.get(run_id, 0):
                raise ValueError("cannot finish a run while requests are in flight")
            if run.kind == "replay":
                return self.session(run_id).finish(
                    outcome,
                    task_success=task_success,
                    detail=detail,
                    findings=findings,
                    evaluation=evaluation,
                )
            runtime = self.faults(run_id)
            return finish_run(
                self.store,
                run,
                outcome,
                finished_at=self.clock(),
                task_success=task_success,
                detail=detail,
                findings=findings,
                evaluation=evaluation,
                observations=RunObservations(
                    exchanges=runtime.observed_exchanges,
                    tool_events=runtime.observed_tools,
                ),
            )
