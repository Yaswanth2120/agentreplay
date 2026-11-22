"""Every frozen task is solved using its instruction, never its expected goal."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from agentreplay.canonical import canonical_json
from agentreplay.evals.envs import ENVIRONMENTS, Environment, make_environment
from agentreplay.evals.make_tasks import (
    GENERATION_SEED,
    generate_tasks,
    main,
    write_jsonl,
)
from agentreplay.evals.tasks import TASKS_PATH, load_tasks, tasks_version
from agentreplay.evals.verifiers import answer_object, verify
from agentreplay.schema import Task

TASKS = load_tasks()


def scripted_solution(
    task: Task, env: Environment, *, wrong: bool = False
) -> tuple[str, int]:
    """Execute the visible instruction; wrong mode changes observed work, not goals."""
    count = 0
    if task.env == "calc":
        workflow = json.loads(task.instruction.split("\nWorkflow: ")[1])
        value = workflow["start"]
        for step in workflow["operations"]:
            if step["tool"] == "convert":
                value = env.call(
                    "convert",
                    {
                        "value": value,
                        "source": step["source"],
                        "target": step["target"],
                    },
                )
            else:
                value = env.call(
                    "calculate", {"op": step["op"], "a": value, "b": step["b"]}
                )
            count += 1
        return json.dumps({"result": value + 1 if wrong else value}), count
    if task.env == "search":
        stations = json.loads(task.instruction.split("\nStations: ")[1])
        codes = []
        for station in stations:
            document = env.call("search", {"query": station})[0]
            codes.append(document["text"].split("access code is ")[1].rstrip("."))
            count += 1
        if wrong:
            codes[-1] = "WRONG"
        return json.dumps({"codes": codes}), count
    operations = json.loads(task.instruction.split("\nWorkflow: ")[1])
    for index, operation in enumerate(operations):
        args = operation["args"].copy()
        if wrong and index == len(operations) - 1:
            continue
        env.call(operation["tool"], args)
        count += 1
    return "Done", count


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_each_task_accepts_correct_and_rejects_wrong_scripted_trajectory(
    task: Task,
) -> None:
    env = make_environment(task.env, task.seed)
    initial = env.state_digest()
    answer, count = scripted_solution(task, env)
    assert verify(task, env.snapshot(), answer)
    assert count == {1: 1, 2: 3, 3: 6}[task.difficulty]
    wrong_env = make_environment(task.env, task.seed)
    wrong_answer, _ = scripted_solution(task, wrong_env, wrong=True)
    assert not verify(task, wrong_env.snapshot(), wrong_answer)
    if task.env in {"calc", "search"}:
        assert env.state_digest() == initial
        assert not verify(task, env.snapshot(), "not JSON")
    else:
        assert env.state_digest() != initial
        assert not verify(
            task, make_environment(task.env, task.seed).snapshot(), "Done"
        )
        damaged = env.snapshot()
        damaged["unrelated_change"] = True
        assert not verify(task, damaged, answer)


def test_task_generation_bytes_hash_counts_difficulty_and_frozen_version(
    tmp_path: Path,
) -> None:
    paths = [tmp_path / "one.jsonl", tmp_path / "two.jsonl"]
    for path in paths:
        assert main(["--out", str(path), "--seed", str(GENERATION_SEED)]) == 0
    assert paths[0].read_bytes() == paths[1].read_bytes() == TASKS_PATH.read_bytes()
    assert (
        hashlib.sha256(paths[0].read_bytes()).digest()
        == hashlib.sha256(paths[1].read_bytes()).digest()
    )
    assert tasks_version(paths[0]) == tasks_version()
    assert tasks_version().startswith("tasks-v1-sha256:")
    assert len(TASKS) == len({task.id for task in TASKS}) == 200
    assert Counter(task.env for task in TASKS) == {name: 40 for name in ENVIRONMENTS}
    for name in ENVIRONMENTS:
        assert len({t.instruction for t in TASKS if t.env == name}) == 40
        assert Counter(t.difficulty for t in TASKS if t.env == name) == {
            1: 16,
            2: 16,
            3: 8,
        }
    changed = tmp_path / "changed.jsonl"
    write_jsonl(changed, [t.model_dump() for t in generate_tasks(GENERATION_SEED + 1)])
    assert tasks_version(changed) != tasks_version()
    assert len(load_tasks(changed)) == 200


@pytest.mark.parametrize("contents", ["", "{}\n", "no JSON\n", "\n"])
def test_invalid_task_files_have_clear_errors(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(contents)
    with pytest.raises(ValueError, match="task"):
        load_tasks(path)


def test_duplicate_ids_and_unknown_task_fields_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.jsonl"
    first = TASKS[0].model_dump()
    write_jsonl(path, [first, first])
    with pytest.raises(ValueError, match="unique IDs"):
        load_tasks(path)
    for key, value in [
        ("difficulty", 4),
        ("env", "network"),
        ("extra", True),
        ("seed", -1),
    ]:
        with pytest.raises(ValueError):
            Task.model_validate({**first, key: value})


@pytest.mark.parametrize(
    "answer",
    [
        "null",
        "[]",
        "NaN",
        "{",
        '{"result": true}',
        '{"result": "5"}',
        '{"result": Infinity}',
    ],
)
def test_numeric_answer_rejects_malformed_and_nonnumeric_values(answer: str) -> None:
    task = TASKS[0].model_copy(deep=True)
    task.verifier.params = {"expected": 5}
    assert not verify(task, {}, answer)
    assert answer_object(answer) is None or isinstance(answer_object(answer), dict)


def test_numeric_tolerance_and_state_types_are_explicit() -> None:
    task = TASKS[0].model_copy(deep=True)
    task.verifier.params = {"expected": 5}
    assert verify(task, {}, '{"result":5.000001}')
    assert not verify(task, {}, '{"result":5.000002}')
    assert verify(task, {}, '{"result":5}')
    state_task = next(t for t in TASKS if t.env == "kv").model_copy(deep=True)
    state_task.verifier.params = {"expected": {"value": 1}}
    assert not verify(state_task, {"value": True}, "")
    assert not verify(state_task, {"value": 1.0}, "")
    assert verify(state_task, {"value": 1}, "")


def test_verifier_configuration_errors_fail_clearly() -> None:
    task = TASKS[0].model_copy(deep=True)
    for params in [
        {},
        {"expected": True},
        {"expected": float("inf")},
        {"expected": 0, "extra": 1},
    ]:
        task.verifier.params = params
        with pytest.raises(ValueError):
            verify(task, {}, "{}")
    for original in [
        next(t for t in TASKS if t.env == "search"),
        next(t for t in TASKS if t.env == "files"),
    ]:
        task = original.model_copy(deep=True)
        task.verifier.params = {"expected": 1}
        with pytest.raises(ValueError):
            verify(task, {}, "{}")
    assert canonical_json(TASKS[0].model_dump())


def test_generation_cli_writes_reproducible_corpus_and_pending_review(
    tmp_path: Path,
) -> None:
    from agentreplay.evals.envs.search import CORPUS_PATH
    from agentreplay.evals.tasks import DATA

    output, corpus, review = (
        tmp_path / "tasks.jsonl",
        tmp_path / "corpus.jsonl",
        tmp_path / "REVIEW.md",
    )
    assert (
        main(
            [
                "--out",
                str(output),
                "--corpus-out",
                str(corpus),
                "--review-out",
                str(review),
            ]
        )
        == 0
    )
    assert output.read_bytes() == TASKS_PATH.read_bytes()
    assert corpus.read_bytes() == CORPUS_PATH.read_bytes()
    assert review.read_bytes() == (DATA.parent / "REVIEW.md").read_bytes()
    rows = [line for line in review.read_text().splitlines() if line.startswith("| ")][
        1:
    ]
    assert len(rows) == 20
    assert all("pending" in row for row in rows)
    assert "Status: PENDING" in review.read_text()
    assert Counter(row.split("|")[1].strip().split("-")[0] for row in rows) == {
        name: 4 for name in ENVIRONMENTS
    }
    assert (
        main(["--out", str(output), "--review-out", str(review), "--seed", "77"]) == 0
    )
    assert "Generation seed: 77" in review.read_text()


def test_verifiers_do_not_import_solutions_environments_faults_or_detectors() -> None:
    import ast

    from agentreplay.evals import verifiers

    tree = ast.parse(Path(verifiers.__file__).read_text())
    modules = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ]
    assert not any(
        part in module.split(".")
        for module in modules
        for part in ["envs", "make_tasks", "detectors", "faults", "store"]
    )


def test_task_generation_does_not_change_global_random_state() -> None:
    import random

    before = random.getstate()
    generate_tasks()
    assert random.getstate() == before


def test_numeric_verifier_rejects_out_of_range_answers_without_overflow() -> None:
    task = TASKS[0].model_copy(deep=True)
    task.verifier.params = {"expected": 5}
    assert not verify(task, {}, '{"result": 1e999}')
    assert not verify(task, {}, json.dumps({"result": 10**500}))
