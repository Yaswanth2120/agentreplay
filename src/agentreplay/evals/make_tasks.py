"""Explicit task/corpus generation command. Changed bytes mean a new eval version."""

from __future__ import annotations

import argparse
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..canonical import canonical_json
from ..schema import Task, TaskVerifier
from .tasks import DATA

GENERATION_SEED = 20261122


def make_corpus() -> list[dict[str, str]]:
    regions = ["north", "south", "east", "west", "central"]
    systems = ["water", "power", "radio", "rail", "weather"]
    return [
        {
            "id": f"doc-{i:03d}",
            "title": f"Station station{i:03d} maintenance",
            "text": f"Station station{i:03d} is in the {regions[i % 5]} region. Its {systems[(i // 5) % 5]} inspection access code is C{(i * 7919 + 1237) % 100000:05d}.",
        }
        for i in range(500)
    ]


def calc_task(rng: random.Random, count: int) -> tuple[str, dict[str, Any]]:
    start = rng.randint(2, 30)
    value = Decimal(start)
    operations: list[dict[str, Any]] = []
    for index in range(count):
        operand = rng.randint(2, 9)
        if index == 1:
            operations.append({"tool": "convert", "source": "m", "target": "cm"})
            value *= 100
        else:
            op = ["add", "multiply", "subtract"][index % 3]
            operations.append({"tool": "calculate", "op": op, "b": operand})
            if op == "add":
                value += operand
            elif op == "multiply":
                value *= operand
            else:
                value -= operand
    instruction = (
        "Start at the given number and apply each operation in order, using the previous result as a/value. "
        'Return only JSON {"result": number}.\nWorkflow: '
        + canonical_json({"start": start, "operations": operations})
    )
    return instruction, {"expected": float(value)}


def kv_task(rng: random.Random, count: int, seed: int) -> tuple[str, dict[str, Any]]:
    state: dict[str, Any] = {"baseline": seed}
    operations: list[dict[str, Any]] = []
    for index in range(count):
        key = f"item-{index}"
        if index == count - 1 and count > 1:
            operations.append({"tool": "delete", "args": {"key": "baseline"}})
            del state["baseline"]
        else:
            value = {
                "quantity": rng.randint(1, 99),
                "tag": rng.choice(["red", "blue", "green"]),
            }
            operations.append({"tool": "set", "args": {"key": key, "value": value}})
            state[key] = value
    return (
        "Apply these key-value operations in order. Preserve all other keys.\nWorkflow: "
        + canonical_json(operations),
        {"expected": state},
    )


def files_task(rng: random.Random, count: int, seed: int) -> tuple[str, dict[str, Any]]:
    state = {"notes/readme.txt": f"Fixture seed {seed}\nPreserve this file.\n"}
    operations = []
    for index in range(count):
        path = f"reports/part-{index}.txt"
        content = f"Batch {rng.randint(100, 999)}\nstatus: ready\n"
        operations.append({"tool": "write", "args": {"path": path, "content": content}})
        state[path] = content
    return (
        "Write these virtual files exactly, preserving existing and unrelated files.\nWorkflow: "
        + canonical_json(operations),
        {"expected": state},
    )


def search_task(rng: random.Random, count: int) -> tuple[str, dict[str, Any]]:
    indices = rng.sample(range(500), count)
    stations = [f"station{i:03d}" for i in indices]
    expected = [f"C{(i * 7919 + 1237) % 100000:05d}" for i in indices]
    return (
        'Find the inspection access code for each station in the supplied order. Return only JSON {"codes": [strings]}.\nStations: '
        + canonical_json(stations),
        {"expected": expected},
    )


def tickets_task(
    rng: random.Random, count: int, seed: int
) -> tuple[str, dict[str, Any]]:
    state: dict[str, Any] = {
        "T0001": {
            "id": "T0001",
            "title": f"Baseline {seed}",
            "priority": "normal",
            "status": "open",
            "assignee": None,
        }
    }
    operations: list[dict[str, Any]] = []
    for index in range(2 if count == 6 else 1):
        id = f"T{index + 2:04d}"
        title = f"Inspect pump {rng.randint(100, 999)}"
        priority = rng.choice(["low", "normal", "high"])
        operations.append(
            {"tool": "create", "args": {"title": title, "priority": priority}}
        )
        ticket = {
            "id": id,
            "title": title,
            "priority": priority,
            "status": "open",
            "assignee": None,
        }
        if count >= 3:
            assignee = rng.choice(["Alex", "Sam", "Riley"])
            operations += [
                {
                    "tool": "update",
                    "args": {"id": id, "assignee": assignee, "status": "in_progress"},
                },
                {"tool": "update", "args": {"id": id, "status": "resolved"}},
            ]
            ticket.update(assignee=assignee, status="resolved")
        state[id] = ticket

    return (
        "Apply this ticket workflow in order. Preserve unrelated tickets.\nWorkflow: "
        + canonical_json(operations),
        {"expected": state},
    )


def generate_tasks(seed: int = GENERATION_SEED) -> list[Task]:
    rng = random.Random(seed)
    tasks = []
    for env in ("calc", "kv", "files", "search", "tickets"):
        seen: set[str] = set()
        for index in range(40):
            difficulty = 1 if index < 16 else 2 if index < 32 else 3
            count = {1: 1, 2: 3, 3: 6}[difficulty]
            while True:
                task_seed = rng.randrange(2**31)
                if env == "calc":
                    instruction, params = calc_task(rng, count)
                elif env == "kv":
                    instruction, params = kv_task(rng, count, task_seed)
                elif env == "files":
                    instruction, params = files_task(rng, count, task_seed)
                elif env == "search":
                    instruction, params = search_task(rng, count)
                else:
                    instruction, params = tickets_task(rng, count, task_seed)
                if instruction not in seen:
                    seen.add(instruction)
                    break
            name = (
                "numeric_answer"
                if env == "calc"
                else "search_answer"
                if env == "search"
                else "exact_state"
            )
            tasks.append(
                Task(
                    id=f"{env}-{index + 1:03d}",
                    env=env,
                    instruction=instruction,
                    seed=task_seed,
                    difficulty=difficulty,
                    verifier=TaskVerifier(name=name, params=params),
                )
            )
    return tasks


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8"
    )


def write_review(path: Path, tasks: list[Task], seed: int = GENERATION_SEED) -> None:
    rng = random.Random(seed)
    selected = []
    for env in ("calc", "kv", "files", "search", "tickets"):
        selected.extend(rng.sample([task for task in tasks if task.env == env], 4))
    lines = [
        "# Owner task-set review",
        "",
        "Status: PENDING — automated scripted solutions do not replace human review.",
        "",
        "Sample: four random tasks per environment, selected with seed "
        + str(seed)
        + ".",
        "Generation seed: "
        + str(seed)
        + ". Review the frozen task file before reading verifier params or detector output.",
        "",
        "For each row, hand-solve from its instruction using the environment tool contracts. Record your actual steps/result,",
        "then inspect the verifier goal separately and flag any mismatch, ambiguity, or unsolvable instruction.",
        "Success-rate reports must not be treated as validated until this review is complete.",
        "",
        "| Task | Difficulty | Hand solution / result | Solvable? | Verifier correct? | Reviewer / date |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {task.id} | {task.difficulty} | pending | pending | pending | pending |"
        for task in selected
    ]
    lines += [
        "",
        "## Findings and corrections",
        "",
        "Pending owner review. A correction to frozen tasks or corpus requires a new evaluation version.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DATA / "tasks.jsonl")
    parser.add_argument("--seed", type=int, default=GENERATION_SEED)
    parser.add_argument(
        "--corpus-out",
        type=Path,
        help="Explicitly regenerate the frozen synthetic corpus too",
    )
    parser.add_argument(
        "--review-out", type=Path, help="Write a pending owner review template"
    )
    args = parser.parse_args(argv)
    write_jsonl(
        args.out, [t.model_dump(mode="json") for t in generate_tasks(args.seed)]
    )
    if args.corpus_out is not None:
        write_jsonl(args.corpus_out, make_corpus())
    if args.review_out is not None:
        write_review(args.review_out, generate_tasks(args.seed), args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
