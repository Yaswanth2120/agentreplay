"""Run-bound fault orchestration; persist labels before exposing mutated values."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from .faults import (
    TERMINAL_FAULTS,
    FaultPlan,
    FaultPlanError,
    FaultRule,
    FaultSelector,
    mutate_exchange,
    mutate_tool,
    schema_validator,
)
from .schema import Exchange, Fault, Run, ToolEvent
from .store import TraceStore


class FaultRuntime:
    def __init__(
        self, store: TraceStore, run: Run, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.store, self.run, self.clock = store, run, clock
        persisted = store.get_run(run.id)
        if (
            persisted is None
            or persisted.finished_at is not None
            or persisted.trace_id != run.trace_id
            or persisted.kind != run.kind
            or persisted.fault_plan_id != run.fault_plan_id
            or persisted.config.get("fault_plan") != run.config.get("fault_plan")
        ):
            raise FaultPlanError(
                "fault runtime does not match an existing unfinished run"
            )
        value = run.config.get("fault_plan")
        self.plan = FaultPlan.from_dict(value) if value is not None else None
        if (self.plan.digest if self.plan is not None else None) != run.fault_plan_id:
            raise FaultPlanError("run fault_plan_id does not match its saved plan")
        self.selector = FaultSelector(self.plan) if self.plan is not None else None
        self._pending: list[tuple[int, FaultRule, ToolEvent]] = []
        self._logged: set[tuple[int, str, int]] = set()
        self._delays: dict[int, float] = {}
        self._lock = threading.RLock()
        self.observed_exchanges: list[Exchange] = []
        self.observed_tools: list[ToolEvent] = []
        self.skipped: list[dict[str, Any]] = []

    def ensure_open(self) -> None:
        run = self.store.get_run(self.run.id)
        if run is None or run.finished_at is not None:
            raise FaultPlanError("fault runtime run is unknown or already finished")

    def _log(self, index: int, rule: FaultRule, seq: int) -> None:
        key = (index, rule.target, seq)
        if key not in self._logged:
            self.store.add_fault(
                Fault(
                    run_id=self.run.id,
                    fault_class=rule.fault_class,
                    target=rule.target,
                    target_seq=seq,
                    params=rule.params,
                    created_at=self.clock(),
                )
            )
            self._logged.add(key)

    def _candidates(self, seq: int) -> list[tuple[int, FaultRule, ToolEvent | None]]:
        selected = (
            self.selector.selected("exchange", seq) if self.selector is not None else []
        )
        candidates = [(index, rule, None) for index, rule in selected]
        candidates.extend(self._pending)
        return sorted(
            candidates,
            key=lambda candidate: (
                candidate[0],
                candidate[2].seq if candidate[2] is not None else seq,
            ),
        )

    def pre_exchange(self, exchange: Exchange) -> Exchange | None:
        with self._lock:
            self.ensure_open()
            candidates = self._candidates(exchange.seq)
            if candidates and candidates[0][1].fault_class in TERMINAL_FAULTS:
                return self.exchange(exchange)
        return None

    def exchange(self, exchange: Exchange) -> Exchange:
        with self._lock:
            self.ensure_open()
            for index, rule, loop in self._candidates(exchange.seq):
                changed = mutate_exchange(exchange, rule, loop)
                if changed is None:
                    self.skipped.append(
                        {
                            "class": rule.fault_class,
                            "target": rule.target,
                            "seq": exchange.seq,
                            "reason": "no eligible response/tool schema",
                        }
                    )
                    continue
                seq = loop.seq if loop is not None else exchange.seq
                self._log(index, rule, seq)
                if loop is not None:
                    self._pending.remove((index, rule, loop))
                self._delays[exchange.seq] = rule.params.get("delay_ms", 0.0)
                self.observe(changed)
                return changed
            self.observe(exchange)
            return exchange

    def observe(self, exchange: Exchange) -> None:
        with self._lock:
            for index, previous in enumerate(self.observed_exchanges):
                if previous.seq == exchange.seq:
                    self.observed_exchanges[index] = exchange.model_copy(deep=True)
                    return
            self.observed_exchanges.append(exchange.model_copy(deep=True))

    def needs_response(self, seq: int) -> bool:
        with self._lock:
            return bool(self._candidates(seq))

    def delay_ms(self, seq: int) -> float:
        return self._delays.get(seq, 0.0)

    def __call__(self, event: ToolEvent, result: Any) -> Any:
        with self._lock:
            self.ensure_open()
            selected = (
                self.selector.selected("tool_event", event.seq)
                if self.selector is not None
                else []
            )
            for index, rule in selected:
                if rule.fault_class == "tool_loop":
                    self._pending.append((index, rule, event.model_copy(deep=True)))
                    return result
                if rule.fault_class == "tool_bad_result" and not schema_validator(
                    rule.params["schema"]
                ).is_valid(event.result):
                    self.skipped.append(
                        {
                            "class": rule.fault_class,
                            "target": "tool_event",
                            "seq": event.seq,
                            "reason": "base result already violates schema",
                        }
                    )
                    continue
                self._log(index, rule, event.seq)
                return mutate_tool(result, rule)
            return result

    @property
    def pending_loops(self) -> list[int]:
        return [event.seq for _, _, event in self._pending]
