"""Versioned trace schema. Bump SCHEMA_VERSION on any breaking change."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import LENIENT_KEYS, STRICT_KEYS, canonical_hash

SCHEMA_VERSION = 7
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


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    trace_id: str | None = None
    kind: Literal["record", "live", "replay"] | None = None
    replay_mode: ReplayMode | None = None
    task_id: str | None = None
    trial: int | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    model: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class FinishRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: RunOutcome = "completed"
    task_success: bool | None = None
    detail: str | None = None


class Fault(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    fault_class: str
    target: Literal["exchange", "tool_event"]
    target_seq: int
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)


class DetectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    timeout_ms: float = Field(default=60000.0, ge=0)
    tool_timeout_ms: float | None = Field(default=None, ge=0)
    max_steps: int = Field(default=25, ge=0)
    loop_repeats: int = Field(default=3, ge=2)
    max_cycle_length: int = Field(default=4, ge=1, le=4)
    loop_window: int = Field(default=24, ge=1)

    @model_validator(mode="after")
    def validate_window(self) -> DetectorConfig:
        if self.loop_window < self.loop_repeats:
            raise ValueError("loop_window must fit at least loop_repeats calls")
        return self


class DetectorRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    trace_id: str
    kind: Literal["record", "live", "replay"]
    outcome: RunOutcome | None = None

    @model_validator(mode="before")
    @classmethod
    def from_run(cls, value: Any) -> Any:
        if isinstance(value, Run):
            return value.model_dump(include=set(cls.model_fields))
        return value


class RunObservations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exchanges: list[Exchange] = Field(default_factory=list)
    tool_events: list[ToolEvent] = Field(default_factory=list)
    exchange_attempts: int | None = Field(default=None, ge=0)


class RunView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: DetectorRun
    exchanges: list[Exchange] = Field(default_factory=list)
    tool_events: list[ToolEvent] = Field(default_factory=list)
    exchange_attempts: int | None = Field(default=None, ge=0)
    config: DetectorConfig = Field(default_factory=DetectorConfig)
    evaluated_at: float = 0.0


class TaskVerifier(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    name: Literal["numeric_answer", "exact_state", "search_answer"]
    params: dict[str, Any]


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    env: Literal["calc", "kv", "files", "search", "tickets"]
    instruction: str = Field(min_length=1)
    seed: int = Field(ge=0)
    difficulty: int = Field(ge=1, le=3)
    verifier: TaskVerifier


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: str
    final_answer: str
    final_state: dict[str, Any]
    state_source: Literal["executed", "reconstructed"]
    state_errors: list[str] = Field(default_factory=list)


class EvaluationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    backend: Literal["scripted", "live"] = "scripted"
    model: str = "scripted-offline"
    upstream_url: str = "http://localhost:8000"
    versions: list[Literal["v1", "v2"]] = Field(default_factory=lambda: ["v1", "v2"])
    trials: int = Field(default=3, ge=3)
    task_ids: list[str] | None = None
    max_steps: int = Field(default=25, ge=1)
    regression_seed: int = 7
    bootstrap_seed: int = 1234
    bootstrap_resamples: int = Field(default=10000, ge=100)
    fault_plan: dict[str, Any] | None = None
    vllm_version: str = "not-used-scripted"
    vllm_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_selection(self) -> EvaluationConfig:
        if not self.versions or len(set(self.versions)) != len(self.versions):
            raise ValueError("versions must be nonempty and unique")
        if self.task_ids is not None and (
            not self.task_ids or len(set(self.task_ids)) != len(self.task_ids)
        ):
            raise ValueError("task_ids must be nonempty and unique")
        if self.fault_plan is not None and "v1" not in self.versions:
            raise ValueError("fault replay needs a v1 source")
        if self.backend == "live" and (
            self.model == "scripted-offline" or self.vllm_version == "not-used-scripted"
        ):
            raise ValueError("live evaluation requires explicit model and vLLM version")
        return self


class EvalLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    kind: Literal["loop", "invalid_tool_call", "timeout"]
    present: bool
    seq: int | None = Field(default=None, ge=0)
    labeler: str = Field(min_length=1)
    created_at: float
