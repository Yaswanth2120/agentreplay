"""Pure observable-data detectors. No storage, network, fault plans, or labels."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from .canonical import canonical_json, tool_args_hash
from .json_validation import parse_arguments, schema_errors
from .schema import Exchange, Finding, RunView

DETECTOR_KINDS = (
    "invalid_tool_call",
    "loop",
    "timeout",
    "malformed_response",
    "llm_error",
    "tool_error",
    "budget_exceeded",
)


def _finding(
    view: RunView,
    kind: str,
    evidence: dict[str, Any],
    *,
    exchange_seq: int | None = None,
    tool_seq: int | None = None,
    severity: Literal["error", "warning"] = "error",
) -> Finding:
    identity = {
        "run_id": view.run.id,
        "kind": kind,
        "exchange_seq": exchange_seq,
        "tool_seq": tool_seq,
        "severity": severity,
        "evidence": evidence,
    }
    return Finding(
        **identity,
        id=hashlib.sha256(canonical_json(identity).encode()).hexdigest(),
        created_at=view.evaluated_at,
    )


def _calls(exchange: Exchange) -> list[tuple[int, Any]]:
    calls = []
    choices = (exchange.response or {}).get("choices", [])
    if not isinstance(choices, list):
        return []
    for choice in choices:
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            continue
        items = choice["message"].get("tool_calls") or []
        if not isinstance(items, list):
            items = [{"invalid_tool_calls_container": items}]
        for call in items:
            calls.append((len(calls), call))
    return calls


def _tool_issue(call: Any, declared: dict[str, Any]) -> str | None:
    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
        return "tool call must contain a function object"
    function = call["function"]
    name = function.get("name")
    if not isinstance(name, str) or name not in declared:
        return f"undefined tool name: {name!r}"[:200]
    raw = function.get("arguments")
    if not isinstance(raw, str):
        return "tool arguments must be a JSON string"
    try:
        arguments = parse_arguments(raw)
    except (ValueError, TypeError) as error:
        return str(error)[:200]
    errors = schema_errors(arguments, declared[name])
    return "; ".join(errors)[:200] if errors else None


def detect_invalid_tool_call(view: RunView) -> list[Finding]:
    findings = []
    for exchange in view.exchanges:
        declared = {}
        tools = exchange.request.get("tools") or []
        for tool in tools if isinstance(tools, list) else []:
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict):
                function = tool["function"]
                if isinstance(function.get("name"), str):
                    declared[function["name"]] = function.get("parameters", {})
        for index, call in _calls(exchange):
            issue = _tool_issue(call, declared)
            if issue is not None:
                findings.append(
                    _finding(
                        view,
                        "invalid_tool_call",
                        {"call_index": index, "reason": issue},
                        exchange_seq=exchange.seq,
                    )
                )
    return findings


def _occurrences(view: RunView) -> list[dict[str, Any]]:
    emitted = []
    pending = list(view.tool_events)
    for exchange in view.exchanges:
        for _, call in _calls(exchange):
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                continue
            function = call["function"]
            name, raw = function.get("name"), function.get("arguments")
            if not isinstance(name, str) or not isinstance(raw, str):
                continue
            signature = (name, tool_args_hash(raw))
            tool_seq = None
            for index, event in enumerate(pending):
                if (event.name, event.args_hash) == signature:
                    tool_seq = pending.pop(index).seq
                    break
            emitted.append(
                {
                    "signature": signature,
                    "exchange_seq": exchange.seq,
                    "tool_seq": tool_seq,
                }
            )
    if emitted:
        return emitted
    return [
        {
            "signature": (event.name, event.args_hash),
            "exchange_seq": None,
            "tool_seq": event.seq,
        }
        for event in view.tool_events
    ]


def detect_loop(view: RunView) -> list[Finding]:
    calls = _occurrences(view)
    config = view.config
    for end in range(1, len(calls) + 1):
        for period in range(1, config.max_cycle_length + 1):
            size = period * config.loop_repeats
            if size > min(end, config.loop_window):
                continue
            block = calls[end - size : end]
            pattern = [call["signature"] for call in block[:period]]
            if all(
                call["signature"] == pattern[index % period]
                for index, call in enumerate(block)
            ):
                return [
                    _finding(
                        view,
                        "loop",
                        {
                            "cycle_length": period,
                            "repeats": config.loop_repeats,
                            "start_call": end - size,
                            "end_call": end - 1,
                            "pattern": [
                                {"name": name, "args_hash": digest}
                                for name, digest in pattern
                            ],
                        },
                        exchange_seq=block[-1]["exchange_seq"],
                        tool_seq=block[0]["tool_seq"],
                    )
                ]
    return []


def _timed_out(exchange: Exchange, view: RunView) -> bool:
    return (
        exchange.status_code in {504, 599}
        or exchange.latency_ms > view.config.timeout_ms
    )


def detect_timeout(view: RunView) -> list[Finding]:
    findings = [
        _finding(
            view,
            "timeout",
            {
                "status_code": exchange.status_code,
                "latency_ms": exchange.latency_ms,
                "threshold_ms": view.config.timeout_ms,
            },
            exchange_seq=exchange.seq,
        )
        for exchange in view.exchanges
        if _timed_out(exchange, view)
    ]
    tool_threshold = (
        view.config.tool_timeout_ms
        if view.config.tool_timeout_ms is not None
        else view.config.timeout_ms
    )
    findings.extend(
        _finding(
            view,
            "timeout",
            {"latency_ms": event.latency_ms, "threshold_ms": tool_threshold},
            tool_seq=event.seq,
        )
        for event in view.tool_events
        if event.latency_ms > tool_threshold
    )
    return findings


def _malformed_reason(exchange: Exchange) -> str | None:
    response = exchange.response
    if response is None:
        return "response unavailable or not parseable"
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return "response requires a nonempty choices list"
    for choice in choices:
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            return "choice requires a message object"
        message = choice["message"]
        if message.get("content") is None and not message.get("tool_calls"):
            return "message has null content and no tool calls"
        if message.get("content") is not None and not isinstance(
            message["content"], str
        ):
            return "message content must be text or null"
    return None


def detect_malformed_response(view: RunView) -> list[Finding]:
    findings = []
    for exchange in view.exchanges:
        if exchange.status_code >= 400:
            continue
        reason = _malformed_reason(exchange)
        if reason is not None:
            findings.append(
                _finding(
                    view,
                    "malformed_response",
                    {"reason": reason},
                    exchange_seq=exchange.seq,
                )
            )
    return findings


def detect_llm_error(view: RunView) -> list[Finding]:
    return [
        _finding(
            view,
            "llm_error",
            {"status_code": exchange.status_code},
            exchange_seq=exchange.seq,
        )
        for exchange in view.exchanges
        if exchange.status_code >= 400 and not _timed_out(exchange, view)
    ]


def detect_tool_error(view: RunView) -> list[Finding]:
    return [
        _finding(view, "tool_error", {"error": event.error[:200]}, tool_seq=event.seq)
        for event in view.tool_events
        if event.error is not None
    ]


def detect_budget_exceeded(view: RunView) -> list[Finding]:
    count = (
        view.exchange_attempts
        if view.exchange_attempts is not None
        else len(view.exchanges)
    )
    if count <= view.config.max_steps:
        return []
    seq = (
        view.exchanges[view.config.max_steps].seq
        if len(view.exchanges) > view.config.max_steps
        else view.config.max_steps
    )
    return [
        _finding(
            view,
            "budget_exceeded",
            {"exchanges": count, "max_steps": view.config.max_steps},
            exchange_seq=seq,
            severity="warning",
        )
    ]


def detect(view: RunView) -> list[Finding]:
    findings = []
    for detector in (
        detect_invalid_tool_call,
        detect_loop,
        detect_timeout,
        detect_malformed_response,
        detect_llm_error,
        detect_tool_error,
        detect_budget_exceeded,
    ):
        findings.extend(detector(view))
    return findings
