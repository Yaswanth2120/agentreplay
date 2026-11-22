"""Load frozen task contracts and fingerprint their exact checked-in bytes."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..schema import Task

DATA = Path(__file__).parent / "data"
TASKS_PATH = DATA / "tasks.jsonl"


def load_tasks(path: str | Path = TASKS_PATH) -> list[Task]:
    tasks: list[Task] = []
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        try:
            tasks.append(Task.model_validate_json(line))
        except ValueError as error:
            raise ValueError(f"invalid task at line {line_number}: {error}") from error
    if not tasks or len({t.id for t in tasks}) != len(tasks):
        raise ValueError("task set must be nonempty and have unique IDs")
    return tasks


def tasks_version(path: str | Path = TASKS_PATH) -> str:
    return "tasks-v1-sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
