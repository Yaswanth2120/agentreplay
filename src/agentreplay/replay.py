"""Deterministic replay matcher.

strict:  request must hash-match (model, messages, tools, sampling params).
         Matched by hash, consumed in recorded order, so identical repeated
         requests (agent loops) replay their recorded responses in sequence.
lenient: match on messages + tools only; sampling/model changes tolerated.

A request that finds no unconsumed match raises ReplayMiss. That IS the
regression signal: the agent's behavior diverged from the recording.
"""

from __future__ import annotations

from typing import Any

from .canonical import LENIENT_KEYS, STRICT_KEYS, canonical_hash, tool_args_hash
from .schema import Exchange, ReplayMode, ToolEvent


class ReplayMiss(Exception):
    def __init__(self, mode: str, request_hash: str, remaining: int) -> None:
        super().__init__(
            f"no recorded exchange for {mode} hash {request_hash[:12]} ({remaining} unconsumed)"
        )
        self.mode, self.request_hash, self.remaining = mode, request_hash, remaining


class Replayer:
    def __init__(self, exchanges: list[Exchange], mode: ReplayMode = "strict") -> None:
        self.mode = mode
        self._keys = STRICT_KEYS if mode == "strict" else LENIENT_KEYS
        self._pending: list[tuple[str, Exchange]] = [
            (ex.strict_hash if mode == "strict" else ex.lenient_hash, ex)
            for ex in exchanges
        ]

    @property
    def remaining(self) -> int:
        return len(self._pending)

    def next(self, request: dict[str, Any]) -> Exchange:
        h = canonical_hash(request, self._keys)
        for i, (eh, ex) in enumerate(self._pending):
            if eh == h:
                del self._pending[i]
                return ex
        raise ReplayMiss(self.mode, h, len(self._pending))


class ToolReplayMiss(Exception):
    def __init__(self, name: str, args_hash: str, remaining: int) -> None:
        self.name, self.args_hash, self.remaining = name, args_hash, remaining
        super().__init__(
            f"no recorded tool event for {name!r} hash {args_hash[:12]} "
            f"({remaining} unconsumed)"
        )


class ToolReplayer:
    def __init__(self, events: list[ToolEvent]) -> None:
        self._pending = sorted(events, key=lambda event: event.seq)

    def next(self, name: str, args: dict[str, Any]) -> ToolEvent:
        digest = tool_args_hash(args)
        for index, event in enumerate(self._pending):
            if event.name == name and event.args_hash == digest:
                return self._pending.pop(index)
        raise ToolReplayMiss(name, digest, len(self._pending))
