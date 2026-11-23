"""Reference agent tool execution through fake HTTP, recording, and offline replay."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from agentreplay.canonical import canonical_json
from agentreplay.evals.envs import ENVIRONMENTS, EnvironmentError, make_environment
from agentreplay.evals.tasks import load_tasks, tasks_version
from agentreplay.evals.verifiers import verify
from agentreplay.proxy import create_app
from agentreplay.schema import Task
from agentreplay.store import TraceStore
from agentreplay.tools import ToolRecorder
from examples.reference_agent import (
    AgentBudgetExceeded,
    execute_call,
    invoke_environment,
    run_agent,
)

TASKS = load_tasks()


def tool_call(name: str, args: dict[str, Any], index: int = 0) -> dict[str, Any]:
    return {
        "id": f"call-{index}",
        "type": "function",
        "function": {"name": name, "arguments": canonical_json(args)},
    }


def reply(message: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", **message},
                    "finish_reason": "tool_calls"
                    if message.get("tool_calls")
                    else "stop",
                }
            ]
        },
    )


class ScriptedModel:
    def __init__(self, task: Task) -> None:
        self.task = task
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        assert body["temperature"] == 0.7 and body["seed"] == 2
        assert body["messages"][1]["content"] == self.task.instruction
        assert "verifier" not in body and "expected" not in body
        results = [
            json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"
        ]
        if self.task.env == "calc":
            workflow = json.loads(self.task.instruction.split("\nWorkflow: ")[1])
            value = results[-1]["result"] if results else workflow["start"]
            if len(results) == len(workflow["operations"]):
                return reply({"content": canonical_json({"result": value})})
            step = workflow["operations"][len(results)]
            args = (
                {"value": value, "source": step["source"], "target": step["target"]}
                if step["tool"] == "convert"
                else {"op": step["op"], "a": value, "b": step["b"]}
            )
            return reply(
                {
                    "content": None,
                    "tool_calls": [tool_call(step["tool"], args, len(results))],
                }
            )
        if self.task.env == "search":
            stations = json.loads(self.task.instruction.split("\nStations: ")[1])
            if len(results) < len(stations):
                return reply(
                    {
                        "content": None,
                        "tool_calls": [
                            tool_call(
                                "search",
                                {"query": stations[len(results)]},
                                len(results),
                            )
                        ],
                    }
                )
            codes = [
                r["result"][0]["text"].split("access code is ")[1].rstrip(".")
                for r in results
            ]
            return reply({"content": canonical_json({"codes": codes})})
        if not results:
            workflow = json.loads(self.task.instruction.split("\nWorkflow: ")[1])
            return reply(
                {
                    "content": None,
                    "tool_calls": [
                        tool_call(step["tool"], step["args"], i)
                        for i, step in enumerate(workflow)
                    ],
                }
            )
        return reply({"content": "Done"})


@pytest.mark.parametrize("env_name", ENVIRONMENTS)
@pytest.mark.parametrize("difficulty", [1, 2, 3])
def test_reference_agent_completes_each_environment_and_difficulty(
    env_name: str, difficulty: int
) -> None:
    task = next(t for t in TASKS if t.env == env_name and t.difficulty == difficulty)
    environment = make_environment(task.env, task.seed)
    model = ScriptedModel(task)

    async def exercise() -> str:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(model), base_url="http://fake"
        ) as client:
            return await run_agent(client, task, environment, trial=2)

    answer = asyncio.run(exercise())
    assert verify(task, environment.snapshot(), answer)
    assert len(model.requests) >= 2


def test_reference_agent_records_and_replays_without_model_or_tool_execution() -> None:
    task = next(t for t in TASKS if t.env == "calc" and t.difficulty == 3)
    model = ScriptedModel(task)

    async def exercise(store: TraceStore) -> None:
        environment = make_environment(task.env, task.seed)
        config = {
            "tasks_version": tasks_version(),
            "task_id": task.id,
            "agent_version": "v1",
        }
        record = create_app(
            store, "record", transport=httpx.MockTransport(model), run_config=config
        )
        run_id = record.state.run_id
        recorder = ToolRecorder(
            store,
            record.state.trace_id,
            "record",
            faults=record.state.runs.faults(run_id),
        )
        async with (
            record.router.lifespan_context(record),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=record), base_url="http://proxy"
            ) as client,
        ):
            answer = await run_agent(
                client, task, environment, trial=2, recorder=recorder, run_id=run_id
            )
            assert verify(task, environment.snapshot(), answer)
            finished = await client.post(
                f"/_agentreplay/runs/{run_id}/finish", json={"task_success": True}
            )
            assert finished.status_code == 200 and finished.json()["findings"] == []
        assert len(store.tool_events(record.state.trace_id)) == 6
        assert len(store.exchanges(record.state.trace_id)) == 7
        recorded_calls = len(model.requests)
        replay = create_app(
            store, "replay", trace_id=record.state.trace_id, run_config=config
        )
        session = replay.state.runs.session(replay.state.run_id)
        replay_recorder = ToolRecorder(
            store, record.state.trace_id, "replay", session=session
        )
        replay_env = make_environment(task.env, task.seed)

        def forbidden(name: str, args: dict[str, Any]) -> None:
            pytest.fail("tool executed during replay")

        replay_env.call = forbidden
        async with (
            replay.router.lifespan_context(replay),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=replay), base_url="http://proxy"
            ) as client,
        ):
            replay_answer = await run_agent(
                client,
                task,
                replay_env,
                trial=2,
                recorder=replay_recorder,
                run_id=session.run.id,
            )
            assert replay_answer == answer
            finished = await client.post(
                f"/_agentreplay/runs/{session.run.id}/finish", json={}
            )
            assert finished.status_code == 200 and finished.json()["findings"] == []
        assert len(model.requests) == recorded_calls

    store = TraceStore()
    try:
        asyncio.run(exercise(store))
    finally:
        store.close()


def test_seeded_v2_regression_changes_state_reproducibly() -> None:
    task = next(t for t in TASKS if t.env == "kv" and t.difficulty == 2)

    async def exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        states = []
        for version in ["v1", "v2", "v2"]:
            env = make_environment(task.env, task.seed)
            model = ScriptedModel(task)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(model), base_url="http://fake"
            ) as client:
                answer = await run_agent(
                    client, task, env, version=version, trial=2, regression_seed=7
                )
            assert verify(task, env.snapshot(), answer) == (version == "v1")
            if version == "v2":
                tool_results = [
                    json.loads(m["content"])
                    for m in model.requests[-1]["messages"]
                    if m["role"] == "tool"
                ]
                assert (
                    sum("skipped batched" in r.get("error", "") for r in tool_results)
                    == 1
                )
            states.append(env.snapshot())
        assert states[1] == states[2] != states[0]
        return states[0], states[1]

    asyncio.run(exercise())


def test_reference_agent_recovery_errors_budget_and_http_failure() -> None:
    task = TASKS[0]
    env = make_environment("calc", task.seed)
    exchanges = 0

    def recovery(request: httpx.Request) -> httpx.Response:
        nonlocal exchanges
        body = json.loads(request.content)
        exchanges += 1
        if exchanges == 1:
            return reply(
                {
                    "content": None,
                    "tool_calls": [
                        tool_call("calculate", {"op": "divide", "a": 1, "b": 0})
                    ],
                }
            )
        assert "division by zero" in body["messages"][-1]["content"]
        return reply({"content": '{"result": 1}'})

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(recovery), base_url="http://fake"
        ) as client:
            assert await run_agent(client, task, env) == '{"result": 1}'
        endless = lambda request: reply(
            {
                "content": None,
                "tool_calls": [tool_call("calculate", {"op": "add", "a": 1, "b": 1})],
            }
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(endless), base_url="http://fake"
        ) as client:
            with pytest.raises(AgentBudgetExceeded, match="2 requests"):
                await run_agent(client, task, env, max_steps=2)
            for kwargs in [{"version": "v3"}, {"max_steps": 0}, {"trial": -1}]:
                with pytest.raises(ValueError):
                    await run_agent(client, task, env, **kwargs)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(429)),
            base_url="http://fake",
        ) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await run_agent(client, task, env)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: reply({"content": None})),
            base_url="http://fake",
        ) as client:
            with pytest.raises(ValueError, match="final assistant"):
                await run_agent(client, task, env)

    asyncio.run(exercise())
    assert "error" in execute_call(env, tool_call("missing", {}), None)
    invalid = tool_call("calculate", {})
    invalid["function"]["arguments"] = "[]"
    assert "error" in execute_call(env, invalid, None)
    invalid["function"]["arguments"] = "{"
    assert "error" in execute_call(env, invalid, None)
    assert invoke_environment(env, "calculate", op="add", a=2, b=3) == 5
    with pytest.raises(EnvironmentError):
        invoke_environment(env, "missing")


def test_v2_skipped_execution_is_reported_as_replay_divergence() -> None:
    task = next(t for t in TASKS if t.env == "kv" and t.difficulty == 2)
    store = TraceStore()

    async def exercise() -> None:
        record = create_app(
            store, "record", transport=httpx.MockTransport(ScriptedModel(task))
        )
        run_id = record.state.run_id
        recorder = ToolRecorder(
            store,
            record.state.trace_id,
            "record",
            faults=record.state.runs.faults(run_id),
        )
        async with (
            record.router.lifespan_context(record),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=record), base_url="http://proxy"
            ) as client,
        ):
            await run_agent(
                client,
                task,
                make_environment(task.env, task.seed),
                trial=2,
                recorder=recorder,
            )
            response = await client.post(f"/_agentreplay/runs/{run_id}/finish", json={})
            assert response.status_code == 200 and response.json()["findings"] == []
        replay = create_app(store, "replay", trace_id=record.state.trace_id)
        session = replay.state.runs.session(replay.state.run_id)
        recorder = ToolRecorder(store, record.state.trace_id, "replay", session=session)
        async with (
            replay.router.lifespan_context(replay),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=replay), base_url="http://proxy"
            ) as client,
        ):
            with pytest.raises(httpx.HTTPStatusError) as error:
                await run_agent(
                    client,
                    task,
                    make_environment(task.env, task.seed),
                    version="v2",
                    trial=2,
                    recorder=recorder,
                )
            assert error.value.response.status_code == 409
            response = await client.post(
                f"/_agentreplay/runs/{session.run.id}/finish",
                json={"outcome": "agent_error"},
            )
            kinds = {f["kind"] for f in response.json()["findings"]}
            assert {"replay_miss", "replay_leftover"} <= kinds

    try:
        asyncio.run(exercise())
    finally:
        store.close()


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_natural_tool_error_recovery_replays_identical_requests(
    store: TraceStore, version: str
) -> None:
    task = TASKS[0]
    requests: list[dict[str, Any]] = []

    def recovery(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        results = [m for m in body["messages"] if m["role"] == "tool"]
        if not results:
            return reply(
                {
                    "content": None,
                    "tool_calls": [
                        tool_call("calculate", {"op": "divide", "a": 1, "b": 0})
                    ],
                }
            )
        error = json.loads(results[0]["content"])["error"]
        assert error.startswith("EnvironmentError:") and "division by zero" in error
        assert "ToolReplayedError" not in error
        if len(results) == 1:
            return reply(
                {
                    "content": None,
                    "tool_calls": [
                        tool_call("calculate", {"op": "add", "a": 2, "b": 3}, 1)
                    ],
                }
            )
        assert json.loads(results[-1]["content"]) == {"result": 5}
        return reply({"content": '{"result": 5}'})

    async def exercise() -> None:
        record = create_app(store, "record", transport=httpx.MockTransport(recovery))
        trace_id = record.state.trace_id
        for app, mode in [(record, "record"), (None, "replay")]:
            app = app or create_app(store, "replay", trace_id=trace_id)
            run_id = app.state.run_id
            recorder = ToolRecorder(
                store,
                trace_id,
                mode,
                faults=app.state.runs.faults(run_id),
                session=app.state.runs.session(run_id) if mode == "replay" else None,
            )
            environment = make_environment(task.env, task.seed)

            def forbidden(name: str, args: dict[str, Any]) -> None:
                pytest.fail("executed tool during error replay")

            if mode == "replay":
                environment.call = forbidden
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://proxy"
                ) as client,
            ):
                answer = await run_agent(
                    client,
                    task,
                    environment,
                    version=version,
                    recorder=recorder,
                    run_id=run_id,
                )
                assert answer == '{"result": 5}'
                finished = await client.post(
                    f"/_agentreplay/runs/{run_id}/finish", json={}
                )
                assert finished.status_code == 200
                findings = finished.json()["findings"]
                assert [f["kind"] for f in findings] == ["tool_error"]
                assert findings[0]["tool_seq"] == 0
        assert len(requests) == 3
        assert len(store.tool_events(trace_id)) == 2
        assert len(store.exchanges(trace_id)) == 3

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "error_type",
    [EnvironmentError, ValueError, TypeError, KeyError, ArithmeticError, OSError],
)
def test_reference_tool_error_text_survives_record_replay(
    store: TraceStore, error_type: type[Exception]
) -> None:
    from agentreplay.schema import Trace

    trace = store.create_trace(Trace(name="errors"))
    environment = make_environment("calc", 0)

    def fail(name: str, args: dict[str, Any]) -> None:
        raise error_type("original failure")

    environment.call = fail
    call = tool_call("calculate", {"op": "add", "a": 1, "b": 2})
    source = execute_call(environment, call, ToolRecorder(store, trace.id, "record"))
    assert source == {
        "error": f"{error_type.__name__}: {error_type('original failure')}"
    }

    def forbidden(name: str, args: dict[str, Any]) -> None:
        pytest.fail("tool executed on replay")

    environment.call = forbidden
    recorder = ToolRecorder(store, trace.id, "replay")
    assert execute_call(environment, call, recorder) == source
    assert recorder.session.finish("completed").outcome == "completed"
