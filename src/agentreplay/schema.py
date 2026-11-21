"""Versioned trace schema. Bump SCHEMA_VERSION on any breaking change."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .canonical import LENIENT_KEYS, STRICT_KEYS, canonical_hash

SCHEMA_VERSION = 4
EXPORT_FORMAT = "agentreplay.trace.v1"


class ToolCall(BaseModel):
    id: str | None = None
    name: str
    arguments: str  # raw JSON string as emitted by the model


class Exchange(BaseModel):
    """One recorded LLM request/response round trip."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    trace_id: str
    seq: int
    request: dict[str, Any]
    strict_hash: str
    lenient_hash: str
    status_code: int
    stream: bool = False
    chunks: list[str] | None = None
    response: dict[str, Any] | None = None
    error: str | None = None
    latency_ms: float = 0.0
    created_at: float = Field(default_factory=time.time)

    @classmethod
    def from_request(
        cls, trace_id: str, seq: int, request: dict[str, Any], **kw: Any
    ) -> Exchange:
        return cls(
            trace_id=trace_id,
            seq=seq,
            request=request,
            strict_hash=canonical_hash(request, STRICT_KEYS),
            lenient_hash=canonical_hash(request, LENIENT_KEYS),
            **kw,
        )

    def tool_calls(self) -> list[ToolCall]:
        if not self.response:
            return []
        out: list[ToolCall] = []
        for choice in self.response.get("choices", []):
            for tc in (choice.get("message") or {}).get("tool_calls") or []:
                fn = tc.get("function", {})
                out.append(
                    ToolCall(
                        id=tc.get("id"),
                        name=fn.get("name", ""),
                        arguments=fn.get("arguments", ""),
                    )
                )
        return out


class Trace(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    name: str
    schema_version: int = SCHEMA_VERSION
    created_at: float = Field(default_factory=time.time)
    task_id: str | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


ReplayMode = Literal["strict", "lenient"]
RunOutcome = Literal["completed", "agent_error", "aborted"]


class Run(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    kind: Literal["record", "live", "replay"]
    trace_id: str
    replay_mode: ReplayMode | None = None
    task_id: str | None = None
    trial: int | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    model: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    fault_plan_id: str | None = None
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    outcome: RunOutcome | None = None
    task_success: bool | None = None
    detail: str | None = None


class Finding(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    kind: str
    severity: Literal["info", "warning", "error"]
    exchange_seq: int | None = None
    tool_seq: int | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)


class ToolEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    trace_id: str
    seq: int
    name: str
    args: dict[str, Any]
    args_hash: str
    result: Any = None
    error: str | None = None
    latency_ms: float = 0.0
    created_at: float = Field(default_factory=time.time)


class TraceRecord(Trace):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    type: Literal["trace"]
    format: Literal["agentreplay.trace.v1"]


class ExchangeRecord(Exchange):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    type: Literal["exchange"]
    seq: int = Field(ge=0)
    latency_ms: float = Field(ge=0)


class ToolRecord(ToolEvent):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    type: Literal["tool_event"]
    seq: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
