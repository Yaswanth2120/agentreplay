"""In-memory tickets with explicit assignment and forward-only status rules."""

from __future__ import annotations

import copy
from typing import Any, ClassVar

from .base import Environment, EnvironmentError, tool_schema

TITLE = {"type": "string", "minLength": 1}
PRIORITY = {"enum": ["low", "normal", "high"]}
STATUS = {"enum": ["open", "in_progress", "resolved"]}


class Tickets(Environment):
    schemas: ClassVar[list[dict[str, Any]]] = [
        tool_schema(
            "create",
            "Create an open ticket; IDs are sequential T0001 etc.",
            {"title": TITLE, "priority": PRIORITY},
            ["title", "priority"],
        ),
        tool_schema(
            "update",
            "Assign, reprioritize, or advance open -> in_progress -> resolved; advancing requires an assignee. Resolved tickets are immutable.",
            {"id": TITLE, "assignee": TITLE, "priority": PRIORITY, "status": STATUS},
            ["id"],
        ),
        tool_schema(
            "query",
            "List tickets by optional status in ID order.",
            {"status": STATUS},
            [],
        ),
    ]

    def __init__(self, seed: int = 0) -> None:
        self.reset(seed)

    def reset(self, seed: int) -> None:
        self.records = {
            "T0001": {
                "id": "T0001",
                "title": f"Baseline {seed}",
                "priority": "normal",
                "status": "open",
                "assignee": None,
            }
        }
        self.next_id = 2

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.records)

    def create(self, title: str, priority: str) -> dict[str, Any]:
        id = f"T{self.next_id:04d}"
        record = {
            "id": id,
            "title": title,
            "priority": priority,
            "status": "open",
            "assignee": None,
        }
        self.records[id] = record
        self.next_id += 1
        return copy.deepcopy(record)

    def update(
        self,
        id: str,
        assignee: str | None = None,
        priority: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        if id not in self.records:
            raise EnvironmentError(f"unknown ticket {id!r}")
        old = self.records[id]
        if old["status"] == "resolved":
            raise EnvironmentError("resolved tickets are immutable")
        changed = {**old}
        if assignee is not None:
            changed["assignee"] = assignee
        if priority is not None:
            changed["priority"] = priority
        if status is not None and status != old["status"]:
            allowed = {"open": "in_progress", "in_progress": "resolved"}
            if allowed.get(old["status"]) != status or not changed["assignee"]:
                raise EnvironmentError("invalid status transition or missing assignee")
            changed["status"] = status
        self.records[id] = changed
        return copy.deepcopy(changed)

    def query(self, status: str | None = None) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(t)
            for _, t in sorted(self.records.items())
            if status is None or t["status"] == status
        ]
