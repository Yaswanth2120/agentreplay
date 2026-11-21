"""Explicit run lifecycle for callers that execute an agent in process."""

from __future__ import annotations

import time
from collections.abc import Callable
from types import TracebackType

from .schema import Run, RunOutcome
from .store import TraceStore


class RunRecorder:
    def __init__(
        self,
        store: TraceStore,
        run: Run,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store, self.run, self.clock = store, run.model_copy(deep=True), clock
        self._entered = False

    def __enter__(self) -> Run:
        if self._entered:
            raise RuntimeError("RunRecorder cannot be reused")
        if self.run.finished_at is not None or self.run.outcome is not None:
            raise ValueError("RunRecorder requires an unfinished run")
        self.run.started_at = self.clock()
        self.store.create_run(self.run)
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
        finished = self.store.finish_run(
            self.run.id,
            outcome,
            finished_at=self.clock(),
            task_success=self.run.task_success,
            detail=f"{exc_type.__name__}: {exc}"
            if exc_type is not None
            else self.run.detail,
        )
        self.run.finished_at = finished.finished_at
        self.run.outcome = finished.outcome
        self.run.detail = finished.detail
