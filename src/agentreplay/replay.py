"""Semantic matching, structured divergence evidence, and per-run consumption."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from .canonical import (
    LENIENT_KEYS,
    STRICT_KEYS,
    canonical_hash,
    canonical_json,
    tool_args_hash,
)
from .detection import finish_run
from .fault_runtime import FaultRuntime
from .schema import (
    Exchange,
    Finding,
    ReplayMode,
    Run,
    RunObservations,
    RunOutcome,
    ToolEvent,
)
from .store import TraceStore


def json_diff(
    before: Any, after: Any, path: str = ""
) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {
        "added": [],
        "removed": [],
        "changed": [],
    }
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before:
                result["added"].append(
                    {"path": child, "value": canonical_json(after[key])[:200]}
                )
            elif key not in after:
                result["removed"].append(
                    {"path": child, "value": canonical_json(before[key])[:200]}
                )
            else:
                diff = json_diff(before[key], after[key], child)
                for kind, values in result.items():
                    values.extend(diff[kind])
    elif isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            child = f"{path}/{index}"
            if index >= len(before):
                result["added"].append(
                    {"path": child, "value": canonical_json(after[index])[:200]}
                )
            elif index >= len(after):
                result["removed"].append(
                    {"path": child, "value": canonical_json(before[index])[:200]}
                )
            else:
                diff = json_diff(before[index], after[index], child)
                for kind, values in result.items():
                    values.extend(diff[kind])
    elif type(before) is not type(after) or before != after:
        result["changed"].append(
            {
                "path": path or "/",
                "before": canonical_json(before)[:200],
                "after": canonical_json(after)[:200],
            }
        )
    return result


class ReplayMiss(Exception):
    def __init__(
        self,
        mode: str,
        request_hash: str,
        remaining: int,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            f"no recorded exchange for {mode} hash {request_hash[:12]} ({remaining} unconsumed)"
        )
        self.mode, self.request_hash, self.remaining = mode, request_hash, remaining
        self.evidence = evidence or {"request_hash": request_hash}


class Replayer:
    def __init__(self, exchanges: list[Exchange], mode: ReplayMode = "strict") -> None:
        if mode not in {"strict", "lenient"}:
            raise ValueError("replay mode must be strict or lenient")
        self.mode = mode
        self._keys = STRICT_KEYS if mode == "strict" else LENIENT_KEYS
        self._recorded = sorted(exchanges, key=lambda exchange: exchange.seq)
        self._pending = list(self._recorded)
        self._attempts = 0
        self.last_order: dict[str, int] | None = None

    @property
    def remaining(self) -> int:
        return len(self._pending)

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def remaining_seqs(self) -> list[int]:
        return [exchange.seq for exchange in self._pending]

    def miss_evidence(self, request: dict[str, Any], position: int) -> dict[str, Any]:
        nearest = next(
            (exchange for exchange in self._recorded if exchange.seq == position), None
        )
        if nearest is None and self._recorded:
            count = len(request.get("messages") or [])
            nearest = min(
                self._recorded,
                key=lambda exchange: (
                    abs(len(exchange.request.get("messages") or []) - count),
                    exchange.seq,
                ),
            )
        return {
            "request_hash": canonical_hash(request, self._keys),
            "position": position,
            "nearest_exchange": {
                "id": nearest.id,
                "seq": nearest.seq,
                "request": nearest.request,
            }
            if nearest is not None
            else None,
            "diff": json_diff(nearest.request if nearest is not None else {}, request),
        }

    def next(self, request: dict[str, Any]) -> Exchange:
        digest = canonical_hash(request, self._keys)
        position = self._attempts
        self._attempts += 1
        self.last_order = None
        for index, exchange in enumerate(self._pending):
            recorded_hash = (
                exchange.strict_hash if self.mode == "strict" else exchange.lenient_hash
            )
            if digest == recorded_hash:
                if index and self.mode == "strict":
                    self.last_order = {
                        "expected_seq": self._pending[0].seq,
                        "actual_seq": exchange.seq,
                    }
                return self._pending.pop(index)
        raise ReplayMiss(
            self.mode, digest, self.remaining, self.miss_evidence(request, position)
        )


class ToolReplayMiss(Exception):
    def __init__(self, name: str, args_hash: str, remaining: int) -> None:
        self.name, self.args_hash, self.remaining = name, args_hash, remaining
        super().__init__(
            f"no recorded tool event for {name!r} hash {args_hash[:12]} ({remaining} unconsumed)"
        )


class ToolReplayer:
    def __init__(self, events: list[ToolEvent]) -> None:
        self._pending = sorted(events, key=lambda event: event.seq)

    @property
    def remaining_seqs(self) -> list[int]:
        return [event.seq for event in self._pending]

    def next(self, name: str, args: dict[str, Any]) -> ToolEvent:
        digest = tool_args_hash(args)
        for index, event in enumerate(self._pending):
            if event.name == name and event.args_hash == digest:
                return self._pending.pop(index)
        raise ToolReplayMiss(name, digest, len(self._pending))


class ReplaySession:
    def __init__(
        self, store: TraceStore, run: Run, *, clock: Callable[[], float] = time.time
    ) -> None:
        if run.kind != "replay":
            raise ValueError("ReplaySession requires a replay run")
        persisted = store.get_run(run.id)
        if (
            persisted is None
            or persisted.finished_at is not None
            or persisted.trace_id != run.trace_id
            or persisted.kind != "replay"
            or persisted.replay_mode != run.replay_mode
        ):
            raise ValueError("replay session must match an existing unfinished run")
        self.store, self.run, self.clock = store, run, clock
        self.faults = FaultRuntime(store, run, clock=clock)
        self.replayer = Replayer(
            store.exchanges(run.trace_id), run.replay_mode or "strict"
        )
        self.tools = ToolReplayer(store.tool_events(run.trace_id))
        self._lock = threading.RLock()
        self._tool_attempts = 0

    def _ensure_open(self) -> None:
        run = self.store.get_run(self.run.id)
        if run is None or run.finished_at is not None:
            raise ValueError(f"run {self.run.id!r} is unknown or already finished")

    def finding(
        self,
        kind: str,
        evidence: dict[str, Any],
        *,
        severity: str = "error",
        exchange_seq: int | None = None,
        tool_seq: int | None = None,
    ) -> Finding:
        finding = Finding(
            run_id=self.run.id,
            kind=kind,
            severity=severity,
            exchange_seq=exchange_seq,
            tool_seq=tool_seq,
            evidence=evidence,
            created_at=self.clock(),
        )
        self.store.add_finding(finding)
        return finding

    def next_exchange(self, request: dict[str, Any]) -> Exchange:
        with self._lock:
            self._ensure_open()
            try:
                exchange = self.replayer.next(request)
            except ReplayMiss as error:
                self.finding(
                    "replay_miss",
                    error.evidence,
                    exchange_seq=error.evidence["position"],
                )
                raise
            if self.replayer.last_order is not None:
                self.finding(
                    "replay_order",
                    self.replayer.last_order,
                    severity="warning",
                    exchange_seq=exchange.seq,
                )
            self.faults.observe(exchange)
            return exchange

    def next_tool(self, name: str, args: dict[str, Any]) -> ToolEvent:
        with self._lock:
            self._ensure_open()
            position = self._tool_attempts
            self._tool_attempts += 1
            try:
                event = self.tools.next(name, args)
                self.faults.observe_tool(event)
                return event
            except ToolReplayMiss as error:
                self.finding(
                    "tool_replay_miss",
                    {
                        "name": name,
                        "args_hash": error.args_hash,
                        "remaining": error.remaining,
                    },
                    tool_seq=position,
                )
                raise

    def finish(
        self,
        outcome: RunOutcome,
        *,
        task_success: bool | None = None,
        detail: str | None = None,
    ) -> Run:
        with self._lock:
            self._ensure_open()
            exchanges, tools = self.replayer.remaining_seqs, self.tools.remaining_seqs
            findings = []
            if exchanges or tools:
                findings.append(
                    Finding(
                        run_id=self.run.id,
                        kind="replay_leftover",
                        severity="error",
                        exchange_seq=exchanges[0] if exchanges else None,
                        tool_seq=tools[0] if tools else None,
                        evidence={"exchange_seqs": exchanges, "tool_seqs": tools},
                        created_at=self.clock(),
                    )
                )
            self.run = finish_run(
                self.store,
                self.run,
                outcome,
                finished_at=self.clock(),
                task_success=task_success,
                detail=detail,
                findings=findings,
                observations=RunObservations(
                    exchanges=self.faults.observed_exchanges,
                    tool_events=self.faults.observed_tools,
                    exchange_attempts=self.replayer.attempts,
                ),
            )
            return self.run
