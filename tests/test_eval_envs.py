"""Behavioral contracts for all five isolated, deterministic environments."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentreplay.evals.envs import ENVIRONMENTS, EnvironmentError, make_environment
from agentreplay.evals.envs.calc import Calculator
from agentreplay.evals.envs.files import Files, safe_path
from agentreplay.evals.envs.kv import KeyValue
from agentreplay.evals.envs.search import CORPUS_PATH, Search, tokens
from agentreplay.evals.envs.tickets import Tickets
from agentreplay.evals.make_tasks import make_corpus
from agentreplay.json_validation import schema_errors


@pytest.mark.parametrize("name", ENVIRONMENTS)
def test_environment_reset_digest_schema_and_isolation(name: str) -> None:
    first, second = make_environment(name, 42), make_environment(name, 42)
    assert first.snapshot() == second.snapshot()
    assert first.state_digest() == second.state_digest()
    assert len(first.state_digest()) == 64
    saved = first.snapshot()
    saved["tamper"] = True
    assert "tamper" not in first.snapshot()
    schemas = first.tools()
    for schema in schemas:
        assert (
            schema_errors({}, schema["function"]["parameters"]) == []
            or schema["function"]["parameters"]["required"]
        )
    schemas[0]["function"]["name"] = "changed"
    assert second.tools()[0]["function"]["name"] != "changed"
    assert first.tools()[0]["function"]["name"] != "changed"
    first.reset(42)
    assert first.snapshot() == second.snapshot()
    with pytest.raises(EnvironmentError, match="unknown tool"):
        first.call("unknown", {})
    with pytest.raises(EnvironmentError, match="invalid arguments"):
        first.call(first.tools()[0]["function"]["name"], {"unrecognized": 1})


def test_unknown_environment() -> None:
    with pytest.raises(EnvironmentError, match="unknown environment"):
        make_environment("network", 0)


@pytest.mark.parametrize(
    ("op", "a", "b", "expected"),
    [
        ("add", 2, 3, 5),
        ("subtract", -2, 3, -5),
        ("multiply", -2, -3, 6),
        ("divide", 7, 2, 3.5),
        ("multiply", 0, 1, 0),
        ("subtract", 0.1, 0.1, 0),
    ],
)
def test_calc_arithmetic(op: str, a: float, b: float, expected: float) -> None:
    assert Calculator().call("calculate", {"op": op, "a": a, "b": b}) == expected


@pytest.mark.parametrize(
    ("source", "target", "value", "expected"),
    [
        ("km", "m", 2.5, 2500),
        ("cm", "m", 250, 2.5),
        ("kg", "g", 0.5, 500),
        ("h", "min", 1.5, 90),
        ("min", "s", 2, 120),
        ("g", "kg", -1000, -1),
        ("m", "m", 0, 0),
    ],
)
def test_calc_conversion(
    source: str, target: str, value: float, expected: float
) -> None:
    assert (
        Calculator().call(
            "convert", {"source": source, "target": target, "value": value}
        )
        == expected
    )


@pytest.mark.parametrize(
    "args",
    [
        {"op": "divide", "a": 1, "b": 0},
        {"op": "power", "a": 2, "b": 3},
        {"op": "add", "a": True, "b": 1},
        {"op": "add", "a": "1", "b": 2},
        {"op": "multiply", "a": 1e308, "b": 1e308},
        {"op": "add", "a": float("inf"), "b": 1},
    ],
)
def test_calc_rejects_errors_and_preserves_state(args: dict) -> None:
    env = Calculator(7)
    digest = env.state_digest()
    with pytest.raises(EnvironmentError):
        env.call("calculate", args)
    assert env.state_digest() == digest
    assert env.call("calculate", {"op": "add", "a": 2, "b": 3}) == 5


def test_calc_bad_units_and_nonfinite_conversion() -> None:
    env = Calculator()
    for source, target in [("m", "kg"), ("no", "s")]:
        with pytest.raises(EnvironmentError):
            env.call("convert", {"value": 1, "source": source, "target": target})
    with pytest.raises(EnvironmentError):
        env.call("convert", {"value": 1e308, "source": "km", "target": "cm"})
    with pytest.raises(EnvironmentError):
        env.calculate("no", 1, 2)


def test_kv_crud_aliasing_missing_null_and_reset() -> None:
    env, other = KeyValue(5), KeyValue(5)
    assert env.call("get", {"key": "baseline"}) == 5
    data = {"nested": [1, None]}
    result = env.call("set", {"key": "z", "value": data})
    result["nested"].append(2)
    data["nested"].append(3)
    assert env.get("z") == {"nested": [1, None]}
    snapshot = env.snapshot()
    snapshot["z"]["nested"].append(4)
    assert env.get("z") == {"nested": [1, None]}
    env.call("set", {"key": "a", "value": None})
    assert env.get("a") is None
    assert env.call("list", {}) == ["a", "baseline", "z"]
    assert env.call("delete", {"key": "a"}) is True
    for name in ("get", "delete"):
        with pytest.raises(EnvironmentError, match="missing key"):
            env.call(name, {"key": "a"})
    env.call("set", {"key": "z", "value": 2})
    assert env.get("z") == 2
    assert other.list() == ["baseline"]
    env.reset(5)
    assert env.state_digest() == other.state_digest()
    env.reset(6)
    assert env.get("baseline") == 6


@pytest.mark.parametrize(
    "path", ["nul\x00path", "/tmp/a", "../x", "a/../x", "./x", "a//b", "a/", "", "a\\b"]
)
def test_virtual_files_reject_unsafe_paths(path: str) -> None:
    env = Files()
    before = env.snapshot()
    with pytest.raises(EnvironmentError):
        env.call("write", {"path": path, "content": "bad"})
    with pytest.raises(EnvironmentError):
        safe_path(path)
    assert env.snapshot() == before


def test_files_read_write_list_grep_and_conflicts(tmp_path: Path) -> None:
    env, other = Files(9), Files(9)
    assert safe_path("a/b.txt") == "a/b.txt"
    assert env.read("notes/readme.txt").startswith("Fixture seed 9")
    env.call("write", {"path": "out/a.txt", "content": "Hello\nHELLO\nHello again\n"})
    assert env.call("read", {"path": "out/a.txt"}) == "Hello\nHELLO\nHello again\n"
    assert env.call("grep", {"text": "Hello"}) == [
        {"path": "out/a.txt", "line": 1, "text": "Hello"},
        {"path": "out/a.txt", "line": 3, "text": "Hello again"},
    ]
    assert env.call("grep", {"text": ".*"}) == []
    assert env.call("list", {}) == ["notes/readme.txt", "out/a.txt"]
    for path in ["out", "out/a.txt/child"]:
        with pytest.raises(EnvironmentError, match="conflicts"):
            env.call("write", {"path": path, "content": "oops"})
    with pytest.raises(EnvironmentError, match="missing file"):
        env.call("read", {"path": "missing"})
    env.write("out/a.txt", "")
    assert env.read("out/a.txt") == ""
    assert not list(tmp_path.iterdir())
    assert other.list() == ["notes/readme.txt"]
    env.reset(9)
    assert env.state_digest() == other.state_digest()


def test_search_fixed_corpus_ranking_tokens_limits_and_aliasing() -> None:
    env = Search()
    assert [
        json.loads(line) for line in CORPUS_PATH.read_text().splitlines()
    ] == make_corpus()
    assert len(env.documents) == 500
    assert tokens("Hello, HELLO station001!") == {"hello", "station001"}
    assert len(env.call("search", {"query": "station", "limit": 50})) == 50
    assert [d["id"] for d in env.search("station", 2)] == ["doc-000", "doc-001"]
    found = env.call("search", {"query": "STATION001 north"})
    assert found == []  # station001 is south, so all tokens really are required.
    found = env.call("search", {"query": "STATION001, maintenance"})
    assert len(found) == 1 and found[0]["id"] == "doc-001"
    found[0]["text"] = "tamper"
    fetched = env.call("get_document", {"id": "doc-001"})
    assert "C09156" in fetched["text"]
    fetched["text"] = "tamper"
    assert "tamper" not in env.get_document("doc-001")["text"]
    digest = env.state_digest()
    env.reset(999)
    assert env.state_digest() == digest
    for args in [
        {"query": "!!!"},
        {"query": ""},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 51},
    ]:
        with pytest.raises(EnvironmentError):
            env.call("search", args)
    with pytest.raises(EnvironmentError, match="unknown document"):
        env.call("get_document", {"id": "missing"})


def test_tickets_create_update_query_business_rules_and_reset() -> None:
    env, other = Tickets(4), Tickets(4)
    ticket = env.call("create", {"title": "Pump", "priority": "high"})
    assert ticket == {
        "id": "T0002",
        "title": "Pump",
        "priority": "high",
        "status": "open",
        "assignee": None,
    }
    ticket["title"] = "tamper"
    before = env.state_digest()
    for args in [
        {"id": "T0002", "status": "resolved"},
        {"id": "T0002", "status": "in_progress"},
        {"id": "missing", "priority": "low"},
    ]:
        with pytest.raises(EnvironmentError):
            env.call("update", args)
        assert env.state_digest() == before
    changed = env.call(
        "update", {"id": "T0002", "assignee": "Sam", "status": "in_progress"}
    )
    assert changed["title"] == "Pump"
    with pytest.raises(EnvironmentError):
        env.call("update", {"id": "T0002", "status": "open"})
    assert env.call("query", {"status": "in_progress"}) == [changed]
    env.call("update", {"id": "T0002", "priority": "low", "status": "resolved"})
    before = env.state_digest()
    with pytest.raises(EnvironmentError, match="immutable"):
        env.call("update", {"id": "T0002", "assignee": "Alex"})
    assert env.state_digest() == before
    rows = env.call("query", {})
    rows[0]["title"] = "tamper"
    assert env.query()[0]["title"] == "Baseline 4"
    assert env.call("create", {"title": "Next", "priority": "normal"})["id"] == "T0003"
    assert len(other.query()) == 1
    env.reset(4)
    assert env.state_digest() == other.state_digest()
    assert env.create("Again", "low")["id"] == "T0002"


@pytest.mark.parametrize(
    ("env", "tool", "args"),
    [
        ("kv", "set", {"key": "", "value": 0}),
        ("kv", "get", {"key": "baseline", "extra": True}),
        ("files", "write", {"path": "x", "content": 1}),
        ("files", "grep", {"text": ""}),
        ("tickets", "create", {"title": "", "priority": "high"}),
        ("tickets", "create", {"title": "x", "priority": "urgent"}),
        ("tickets", "update", {"id": "T0001", "status": "closed"}),
        ("tickets", "update", {"id": "T0001", "assignee": ""}),
        ("tickets", "update", {"id": "T0001", "title": "new"}),
        ("search", "search", {"query": "x", "limit": True}),
    ],
)
def test_schema_rejections_never_mutate(env: str, tool: str, args: dict) -> None:
    instance = make_environment(env, 12)
    before = instance.state_digest()
    with pytest.raises(EnvironmentError):
        instance.call(tool, args)
    assert instance.state_digest() == before


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), {"x": float("inf")}, {1: "key"}, (1, 2), object()],
)
def test_non_json_tool_values_fail_without_corrupting_state(value: object) -> None:
    env = KeyValue()
    digest = env.state_digest()
    with pytest.raises(EnvironmentError, match="JSON"):
        env.call("set", {"key": "x", "value": value})
    assert env.state_digest() == digest
