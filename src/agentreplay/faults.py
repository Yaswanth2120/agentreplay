"""Versioned fault plans, canonical seeded selection, and pure mutations."""

from __future__ import annotations

import copy
import hashlib
import json
import random
import threading
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import canonical_json
from .schema import Exchange, ToolEvent
from .streaming import synthesize_chunks

FAULT_DEFAULTS: dict[str, dict[str, Any]] = {
    "llm_timeout": {"delay_ms": 301000.0},
    "llm_http_error": {"status": 500},
    "llm_malformed_json": {},
    "llm_empty_response": {},
    "tool_args_corrupt": {},
    "tool_wrong_name": {"name": "__undefined_tool__"},
    "tool_args_schema_violation": {},
    "tool_loop": {"repeat": 5},
    "tool_exec_timeout": {"message": "injected tool timeout"},
    "tool_exec_error": {"exception": "RuntimeError", "message": "injected tool error"},
    "tool_bad_result": {"value": None},
}
TOOL_ERRORS: dict[str, type[Exception]] = {
    "RuntimeError": RuntimeError,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "OSError": OSError,
    "KeyError": KeyError,
    "TimeoutError": TimeoutError,
    "ConnectionError": ConnectionError,
}
TERMINAL_FAULTS = {"llm_timeout", "llm_http_error"}


class FaultPlanError(ValueError):
    """Invalid fault plan, parameters, or incompatible runtime configuration."""


class FaultSelect(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    seq: list[int] | None = None
    probability: float | None = Field(default=None, ge=0, le=1)
    every_nth: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_selection(self) -> FaultSelect:
        if (
            sum(
                value is not None
                for value in (self.seq, self.probability, self.every_nth)
            )
            != 1
        ):
            raise ValueError(
                "select requires exactly one of seq, probability, every_nth"
            )
        if self.seq is not None and (
            not self.seq or min(self.seq) < 0 or len(set(self.seq)) != len(self.seq)
        ):
            raise ValueError("seq requires unique nonnegative sequence numbers")
        return self


class FaultRule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
    fault_class: str = Field(alias="class")
    target: Literal["exchange", "tool_event"]
    select: FaultSelect
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_rule(self) -> FaultRule:
        if self.fault_class not in FAULT_DEFAULTS:
            raise ValueError(f"unknown fault class {self.fault_class!r}")
        tool_class = self.fault_class in {
            "tool_exec_timeout",
            "tool_exec_error",
            "tool_bad_result",
        }
        if (
            self.fault_class != "tool_loop"
            and (self.target == "tool_event") != tool_class
        ):
            raise ValueError(
                f"{self.fault_class} has incompatible target {self.target}"
            )
        defaults = FAULT_DEFAULTS[self.fault_class]
        allowed = set(defaults) | (
            {"schema"} if self.fault_class == "tool_bad_result" else set()
        )
        if self.params.keys() - allowed:
            raise ValueError(
                f"unknown params for {self.fault_class}: {sorted(self.params.keys() - allowed)}"
            )
        self.params = copy.deepcopy({**defaults, **self.params})
        validate_params(self.fault_class, self.params)
        return self


def schema_validator(schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
    pending: list[Any] = [schema]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if (
                    key in {"$ref", "$dynamicRef"}
                    and isinstance(value, str)
                    and not value.startswith("#")
                ):
                    raise FaultPlanError(
                        "external schema references are unsupported in offline faults"
                    )
                pending.append(value)
        elif isinstance(item, list):
            pending.extend(item)
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def validate_params(kind: str, params: dict[str, Any]) -> None:
    if json.loads(json.dumps(params, allow_nan=False)) != params:
        raise ValueError(
            "fault params must use JSON-native values and string object keys"
        )
    if kind == "llm_http_error" and (
        type(params["status"]) is not int or not 400 <= params["status"] <= 599
    ):
        raise ValueError("status must be an integer from 400 to 599")
    if kind == "llm_timeout" and (
        type(params["delay_ms"]) not in {int, float} or params["delay_ms"] <= 0
    ):
        raise ValueError("delay_ms must be positive")
    if kind == "tool_loop" and (
        type(params["repeat"]) is not int or params["repeat"] < 3
    ):
        raise ValueError("repeat must be an integer >= 3")
    for key in ("name", "message"):
        if key in params and (not isinstance(params[key], str) or not params[key]):
            raise ValueError(f"{key} must be a nonempty string")
    if kind == "tool_exec_error" and params["exception"] not in TOOL_ERRORS:
        raise ValueError(f"unsupported tool exception {params['exception']!r}")
    if kind == "tool_bad_result":
        schema = params.get("schema")
        if not isinstance(schema, dict):
            raise ValueError("tool_bad_result requires a declared result schema")
        if schema_validator(schema).is_valid(params["value"]):
            raise ValueError(
                "tool_bad_result value must violate its declared result schema"
            )


class FaultPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    seed: int
    faults: list[FaultRule]

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, version: Any) -> int:
        if type(version) is not int or version != 1:
            raise ValueError("unsupported fault plan version; expected 1")
        return version

    @classmethod
    def from_dict(cls, value: Any) -> FaultPlan:
        try:
            return cls.model_validate(value)
        except (ValueError, TypeError, jsonschema.SchemaError) as error:
            raise FaultPlanError(f"invalid fault plan: {error}") from error

    @classmethod
    def load(cls, path: str | Path) -> FaultPlan:
        try:
            value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise FaultPlanError(f"cannot load fault plan {path}: {error}") from error
        return cls.from_dict(value)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(self.model_dump(by_alias=True)).encode()
        ).hexdigest()


def plan_config(plan: FaultPlan | None) -> dict[str, Any]:
    return {"fault_plan": plan.model_dump(by_alias=True)} if plan is not None else {}


class FaultSelector:
    def __init__(self, plan: FaultPlan) -> None:
        self.plan = plan.model_copy(deep=True)
        self._random = random.Random(plan.seed)
        self._draws: list[dict[int, float]] = []
        self._lock = threading.RLock()

    def selected(self, target: str, seq: int) -> list[tuple[int, FaultRule]]:
        if seq < 0:
            raise ValueError("fault selection sequence must be nonnegative")
        with self._lock:
            while len(self._draws) <= seq:
                self._draws.append(
                    {
                        index: self._random.random()
                        for index, rule in enumerate(self.plan.faults)
                        if rule.select.probability is not None
                    }
                )
            selected = []
            for index, rule in enumerate(self.plan.faults):
                choose = rule.select
                matches = (
                    seq in choose.seq
                    if choose.seq is not None
                    else (seq + 1) % choose.every_nth == 0
                    if choose.every_nth is not None
                    else self._draws[seq][index] < choose.probability
                )
                if rule.target == target and matches:
                    selected.append((index, rule))
            return selected


def replace_response(
    exchange: Exchange,
    response: dict[str, Any] | None,
    *,
    status: int | None = None,
    raw: str | None = None,
) -> Exchange:
    changed = exchange.model_copy(deep=True)
    changed.response, changed.error = response, raw
    changed.status_code = status if status is not None else exchange.status_code
    changed.chunks = (
        (["data: " + raw] if raw is not None else synthesize_chunks(response))
        if changed.stream and (response is not None or raw is not None)
        else None
    )
    if changed.status_code >= 400:
        changed.chunks = None
    return changed


def invalid_arguments(schema: dict[str, Any]) -> str | None:
    try:
        validator = schema_validator(schema)
        for value in (None, [], {}, "invalid", 0, True):
            if not validator.is_valid(value):
                return canonical_json(value)
    except jsonschema.SchemaError as error:
        raise FaultPlanError(f"invalid declared tool schema: {error}") from error
    return None


def mutate_exchange(
    exchange: Exchange, rule: FaultRule, loop: ToolEvent | None = None
) -> Exchange | None:
    kind, params = rule.fault_class, rule.params
    if kind in TERMINAL_FAULTS:
        status = 504 if kind == "llm_timeout" else params["status"]
        return replace_response(
            exchange,
            {"error": {"type": kind, "message": "injected LLM failure"}},
            status=status,
        )
    if exchange.response is None or exchange.status_code >= 400:
        return None
    response = copy.deepcopy(exchange.response)
    if kind == "llm_malformed_json":
        raw = canonical_json(response)
        return replace_response(exchange, None, raw=raw[: max(1, len(raw) // 2)])
    if kind == "llm_empty_response":
        response["choices"] = []
        return replace_response(exchange, response)
    choices = response.get("choices") or []
    if not choices:
        return None
    message = choices[0].get("message") or {}
    calls = message.get("tool_calls") or []
    if kind == "tool_loop" and loop is not None:
        calls = [
            {
                "id": f"loop-{loop.seq}",
                "type": "function",
                "function": {
                    "name": loop.name,
                    "arguments": canonical_json(loop.args),
                },
            }
        ]
    if not calls:
        return None
    function = calls[0]["function"]
    if kind == "tool_args_corrupt":
        function["arguments"] = '{"injected":'
    elif kind == "tool_wrong_name":
        names = {
            tool.get("function", {}).get("name")
            for tool in exchange.request.get("tools", [])
        }
        name = params["name"]
        while name in names:
            name += "_"
        function["name"] = name
    elif kind == "tool_args_schema_violation":
        schema = next(
            (
                tool["function"].get("parameters", {})
                for tool in exchange.request.get("tools", [])
                if tool.get("function", {}).get("name") == function["name"]
            ),
            {},
        )
        arguments = invalid_arguments(schema)
        if arguments is None:
            return None
        function["arguments"] = arguments
    elif kind == "tool_loop":
        message["tool_calls"] = []
        for index in range(params["repeat"]):
            call = copy.deepcopy(calls[0])
            call["id"] = f"{call.get('id', 'call')}-repeat-{index}"
            message["tool_calls"].append(call)
        message["content"] = None
        choices[0]["finish_reason"] = "tool_calls"
    else:
        return None
    choices[0]["message"] = message
    return replace_response(exchange, response)


def mutate_tool(result: Any, rule: FaultRule) -> Any:
    if rule.fault_class == "tool_exec_timeout":
        raise TimeoutError(rule.params["message"])
    if rule.fault_class == "tool_exec_error":
        raise TOOL_ERRORS[rule.params["exception"]](rule.params["message"])
    if rule.fault_class == "tool_bad_result":
        return copy.deepcopy(rule.params["value"])
    return result
