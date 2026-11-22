"""Execute tasks through the real proxy; statistics remain in stats.py."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..faults import FaultPlan
from ..proxy import create_app
from ..replay import ToolReplayMiss
from ..schema import EvaluationConfig, EvaluationResult, Finding, Run, Task, Trace
from ..store import TraceStore
from ..tools import ToolRecorder
from .agent import run_agent
from .envs import EnvironmentError, make_environment
from .scripted import scripted_response
from .tasks import load_tasks, tasks_version
from .verifiers import verify


def load_config(path: str | Path) -> EvaluationConfig:
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid evaluation YAML: {error}") from error
    return EvaluationConfig.model_validate(value)


def git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        )
    except OSError:
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def reconstruct_state(
    store: TraceStore, source: Run, observed_seqs: list[int], task: Task
) -> tuple[dict[str, Any], list[str]]:
    environment = make_environment(task.env, task.seed)
    source_events = {event.seq: event for event in store.tool_events(source.trace_id)}
    errors = []
    for seq in observed_seqs:
        event = source_events[seq]
        if event.error is None:
            try:
                environment.call(event.name, event.args)
            except EnvironmentError as error:
                errors.append(f"tool {seq}: {error}")
    return environment.snapshot(), errors


async def run_task(
    store: TraceStore,
    task: Task,
    trial: int,
    version: str,
    config: EvaluationConfig,
    metadata: dict[str, Any],
    *,
    phase: str = "record",
    source: Run | None = None,
    repeat: int | None = None,
    plan: FaultPlan | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.time,
) -> Run:
    run_config = {
        **metadata,
        "phase": phase,
        "source_run_id": source.id if source else None,
        "repeat": repeat,
        "task_id": task.id,
        "trial": trial,
        "agent_name": "reference_agent",
        "agent_version": version,
        "model": config.model,
        "sampling": {"temperature": 0.7, "seed": trial},
        "task": task.model_dump(),
        "detectors": {"max_steps": config.max_steps},
    }
    if source is None:
        trace = store.create_trace(
            Trace(
                name=f"{task.id}-{version}-{trial}",
                task_id=task.id,
                agent_name="reference_agent",
                agent_version=version,
                model=config.model,
                created_at=clock(),
            )
        )
        if transport is None and config.backend == "scripted":
            transport = httpx.MockTransport(scripted_response)
    else:
        trace = store.get_trace(source.trace_id)
    mode = "replay" if source else "live" if config.backend == "live" else "record"
    app = create_app(
        store,
        mode,
        trace_id=trace.id,
        upstream_url=config.upstream_url,
        run_config=run_config,
        faults=plan,
        transport=transport,
        wall_clock=clock,
    )
    run_id = app.state.run_id
    runtime = app.state.runs.faults(run_id)
    recorder = ToolRecorder(
        store,
        trace.id,
        "record" if source is None else "replay",
        faults=runtime,
        session=app.state.runs.session(run_id) if source else None,
    )
    environment = make_environment(task.env, task.seed)
    finished = False
    answer, detail, outcome = "", None, "completed"
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://evaluation-proxy",
            ) as client,
        ):
            try:
                answer = await run_agent(
                    client,
                    task,
                    environment,
                    model=config.model,
                    version=version,
                    trial=trial,
                    regression_seed=config.regression_seed,
                    max_steps=config.max_steps,
                    recorder=recorder,
                    run_id=run_id,
                )
            except (
                ToolReplayMiss,
                httpx.HTTPError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                ArithmeticError,
                RuntimeError,
                OSError,
            ) as error:
                detail, outcome = f"{type(error).__name__}: {error}", "agent_error"
        state, state_errors = (
            (environment.snapshot(), [])
            if source is None
            else reconstruct_state(
                store, source, [event.seq for event in runtime.observed_tools], task
            )
        )
        success = not state_errors and verify(task, state, answer)
        failures = (
            []
            if success
            else [
                Finding(
                    run_id=run_id,
                    kind="task_failure",
                    severity="error",
                    evidence={
                        "task_id": task.id,
                        "verifier": task.verifier.name,
                        "state_errors": state_errors,
                    },
                    created_at=clock(),
                )
            ]
        )
        result = EvaluationResult(
            run_id=run_id,
            final_answer=answer,
            final_state=state,
            state_source="executed" if source is None else "reconstructed",
            state_errors=state_errors,
        )
        run = app.state.runs.finish(
            run_id,
            outcome,
            task_success=success,
            detail=detail,
            findings=failures,
            evaluation=result,
        )
        finished = True
        return run
    finally:
        if not finished:
            pending = sys.exc_info()[1]
            aborted = isinstance(
                pending,
                (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError),
            )
            app.state.runs.finish(
                run_id,
                "aborted" if aborted else "agent_error",
                detail=f"evaluation infrastructure failure: {type(pending).__name__}: {pending}",
            )


async def evaluate(
    store: TraceStore,
    config: EvaluationConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.time,
    git_sha: str | None = None,
) -> dict[str, Any]:
    tasks = load_tasks()
    selected = set(config.task_ids or [task.id for task in tasks])
    if selected - {task.id for task in tasks}:
        raise ValueError(
            "unknown task IDs: "
            + ", ".join(sorted(selected - {task.id for task in tasks}))
        )
    tasks = [task for task in tasks if task.id in selected]
    plan = (
        FaultPlan.model_validate(config.fault_plan)
        if config.fault_plan is not None
        else None
    )
    evaluation_id = uuid.uuid4().hex
    metadata = {
        "evaluation_id": evaluation_id,
        "evaluation_config": {
            **config.model_dump(),
            "task_ids": [task.id for task in tasks],
        },
        "tasks_version": tasks_version(),
        "git_sha": git_sha or git_revision(),
        "vllm_version": config.vllm_version,
        "vllm_flags": config.vllm_flags,
        "evaluation_started_at": clock(),
        "owner_review": "pending",
    }
    sources = []
    for version in config.versions:
        for task in tasks:
            for trial in range(config.trials):
                source = await run_task(
                    store,
                    task,
                    trial,
                    version,
                    config,
                    metadata,
                    transport=transport,
                    clock=clock,
                )
                sources.append((task, source))
    ids = [source.id for _, source in sources]
    for task, source in sources:
        clean = source.task_success is True and not store.findings(source.id)
        for repeat in range(2):
            run = await run_task(
                store,
                task,
                source.trial,
                source.agent_version,
                config,
                {**metadata, "clean_source": clean},
                phase="clean_replay",
                source=source,
                repeat=repeat,
                clock=clock,
            )
            ids.append(run.id)
        if source.agent_version == "v1" and "v2" in config.versions:
            run = await run_task(
                store,
                task,
                source.trial,
                "v2",
                config,
                metadata,
                phase="regression_replay",
                source=source,
                clock=clock,
            )
            ids.append(run.id)
        if source.agent_version == "v1" and clean and plan is not None:
            run = await run_task(
                store,
                task,
                source.trial,
                "v1",
                config,
                metadata,
                phase="fault_replay",
                source=source,
                plan=plan,
                clock=clock,
            )
            ids.append(run.id)
    return {"evaluation_id": evaluation_id, "run_ids": ids, "backend": config.backend}


def run_evaluation(store: TraceStore, config: EvaluationConfig) -> dict[str, Any]:
    return asyncio.run(evaluate(store, config))
