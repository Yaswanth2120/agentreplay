import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_faults import make_plan, tool_exchange

from agentreplay.canonical import canonical_json, tool_args_hash
from agentreplay.cli import main
from agentreplay.evals.faults import evaluate_faults
from agentreplay.exportio import import_trace
from agentreplay.inspection import render_trace
from agentreplay.proxy import create_app
from agentreplay.schema import Exchange, ToolEvent, Trace
from agentreplay.store import TraceStore
from agentreplay.streaming import reassemble, synthesize_chunks
from agentreplay.tools import ToolRecorder

GOLDEN = Path(__file__).parents[1] / "golden" / "minimal-add.jsonl"


def seed_fault_trace(store: TraceStore, stream: bool = False) -> tuple[str, Exchange]:
    trace = store.create_trace(Trace(name="source"))
    source = tool_exchange(trace.id)
    source = source.model_copy(
        update={
            "stream": stream,
            "request": {**source.request, "stream": stream},
            "chunks": synthesize_chunks(source.response) if stream else None,
        }
    )
    store.add_exchange(source)
    return trace.id, source


@pytest.mark.parametrize("kind,status", [("llm_timeout", 504), ("llm_http_error", 429)])
def test_replay_terminal_faults_are_instant_logged_and_leave_source_unchanged(
    store: TraceStore,
    kind: str,
    status: int,
) -> None:
    trace_id, source = seed_fault_trace(store)
    before = canonical_json(
        [exchange.model_dump() for exchange in store.exchanges(trace_id)]
    )
    plan = make_plan(kind, params={"status": 429} if kind == "llm_http_error" else {})

    async def no_sleep(seconds: float) -> None:
        pytest.fail("replay slept")

    def no_upstream(request: httpx.Request) -> httpx.Response:
        pytest.fail("replay accessed upstream")

    with TestClient(
        create_app(
            store,
            "replay",
            trace_id=trace_id,
            faults=plan,
            sleep=no_sleep,
            transport=httpx.MockTransport(no_upstream),
        )
    ) as client:
        reply = client.post("/v1/chat/completions", json=source.request)
        assert reply.status_code == status and reply.json()["error"]["type"] == kind
        run = store.get_run(client.app.state.run_id)
        assert (
            run.fault_plan_id == plan.digest and run.config["fault_plan"]["seed"] == 17
        )
        assert store.faults(run.id)[0].fault_class == kind
        client.post(f"/_agentreplay/runs/{run.id}/finish", json={})
    assert (
        canonical_json(
            [exchange.model_dump() for exchange in store.exchanges(trace_id)]
        )
        == before
    )


@pytest.mark.parametrize("kind", ["llm_timeout", "llm_http_error"])
def test_record_terminal_fault_precedes_upstream_and_is_logged_before_delay(
    store: TraceStore, kind: str
) -> None:
    delays = []
    params = {"delay_ms": 200.0} if kind == "llm_timeout" else {"status": 500}
    plan = make_plan(kind, params=params)
    ticks = iter([10.0, 10.5])
    app = None

    async def fake_sleep(seconds: float) -> None:
        assert store.faults(app.state.run_id)[0].fault_class == kind
        delays.append(seconds)

    app = create_app(
        store,
        "live",
        faults=plan,
        sleep=fake_sleep,
        clock=lambda: next(ticks),
        transport=httpx.MockTransport(
            lambda request: pytest.fail("preflight fault contacted upstream")
        ),
    )
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=tool_exchange().request)
        assert response.status_code == (504 if kind == "llm_timeout" else 500)
        exchange = store.exchanges(app.state.trace_id)[0]
        assert (
            exchange.status_code == response.status_code
            and exchange.latency_ms == 500.0
        )
        assert len(store.faults(app.state.run_id)) == 1
    assert delays == ([0.2] if kind == "llm_timeout" else [])


@pytest.mark.parametrize("mode", ["record", "replay"])
@pytest.mark.parametrize(
    "kind", ["llm_empty_response", "tool_args_corrupt", "llm_malformed_json"]
)
def test_stream_faults_are_delivered_and_recorded_in_the_observed_shape(
    store: TraceStore,
    mode: str,
    kind: str,
) -> None:
    plan = make_plan(kind)
    if mode == "replay":
        trace_id, source = seed_fault_trace(store, stream=True)
        app = create_app(store, mode, trace_id=trace_id, faults=plan)
    else:
        source = tool_exchange()
        chunks = synthesize_chunks(source.response)
        wire = "\n\n".join(chunks) + "\n\n"
        app = create_app(
            store,
            mode,
            faults=plan,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, text=wire, headers={"content-type": "text/event-stream"}
                )
            ),
        )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions", json={**source.request, "stream": True}
        )
        assert response.status_code == 200 and response.headers[
            "content-type"
        ].startswith("text/event-stream")
        runtime = app.state.runs.faults(app.state.run_id)
        assert store.faults(app.state.run_id)[0].fault_class == kind
        if kind == "llm_malformed_json":
            assert runtime.observed_exchanges[0].response is None
            line = next(
                line for line in response.text.splitlines() if line.startswith("data:")
            )
            with pytest.raises(json.JSONDecodeError):
                json.loads(line[5:])
        else:
            assembled = reassemble(
                [
                    line
                    for line in response.text.splitlines()
                    if line.startswith("data:")
                ]
            )
            if kind == "llm_empty_response":
                assert assembled["choices"] == []
            else:
                arguments = assembled["choices"][0]["message"]["tool_calls"][0][
                    "function"
                ]["arguments"]
                with pytest.raises(json.JSONDecodeError):
                    json.loads(arguments)
        if mode == "record":
            recorded = store.exchanges(app.state.trace_id)[0]
            assert recorded.response == runtime.observed_exchanges[0].response
            assert recorded.chunks == runtime.observed_exchanges[0].chunks


@pytest.mark.parametrize("mode", ["record", "replay"])
def test_tool_loop_hook_is_shared_with_proxy_and_has_real_tool_target(
    store: TraceStore, mode: str
) -> None:
    trace_id, first = seed_fault_trace(store)
    second = Exchange.from_request(
        trace_id,
        1,
        {**first.request, "messages": [{"role": "user", "content": "finish"}]},
        status_code=200,
        response={
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ]
        },
    )
    if mode == "replay":
        store.add_exchange(second)
        store.add_tool_event(
            ToolEvent(
                trace_id=trace_id,
                seq=0,
                name="add",
                args={"a": 1},
                args_hash=tool_args_hash({"a": 1}),
                result=1,
            )
        )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json=first.response if len(calls) == 1 else second.response
        )

    app = create_app(
        store,
        mode,
        trace_id=trace_id if mode == "replay" else None,
        faults=make_plan("tool_loop", target="tool_event"),
        transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        assert (
            client.post("/v1/chat/completions", json=first.request).status_code == 200
        )
        runtime = app.state.runs.faults(app.state.run_id)
        recorder = ToolRecorder(
            store,
            app.state.trace_id,
            mode,
            faults=runtime if mode == "record" else None,
            session=app.state.session if mode == "replay" else None,
        )
        assert (
            recorder.call(
                "add",
                {"a": 1},
                lambda a: a if mode == "record" else pytest.fail("executed"),
            )
            == 1
        )
        assert store.faults(app.state.run_id) == []
        reply = client.post("/v1/chat/completions", json=second.request)
        assert len(reply.json()["choices"][0]["message"]["tool_calls"]) == 5
        row = store.faults(app.state.run_id)[0]
        assert row.target == "tool_event" and row.target_seq == 0
        assert row.fault_class == "tool_loop" and runtime.pending_loops == []
        assert "Fault: tool_loop" in render_trace(store, app.state.trace_id)
    assert len(calls) == (2 if mode == "record" else 0)


def test_control_runs_can_override_or_disable_default_plan(store: TraceStore) -> None:
    trace_id, source = seed_fault_trace(store)
    default = make_plan("llm_http_error", params={"status": 500})
    override = make_plan("llm_http_error", params={"status": 429})
    with TestClient(
        create_app(store, "replay", trace_id=trace_id, faults=default)
    ) as client:
        for value, expected in [(override.model_dump(by_alias=True), 429), (None, 200)]:
            created = client.post(
                "/_agentreplay/runs", json={"config": {"fault_plan": value}}
            )
            run_id = created.json()["run_id"]
            response = client.post(
                "/v1/chat/completions",
                json=source.request,
                headers={"X-AgentReplay-Run": run_id},
            )
            assert response.status_code == expected
            assert store.get_run(run_id).fault_plan_id == (
                override.digest if value is not None else None
            )
        count = len(store.runs(trace_id))
        assert (
            client.post(
                "/_agentreplay/runs", json={"config": {"fault_plan": {"version": 999}}}
            ).status_code
            == 400
        )
        assert len(store.runs(trace_id)) == count


def test_fault_eval_cli_runs_fixed_trajectory_without_scoring_or_mutating_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "eval.db"
    store = TraceStore(path)
    trace = import_trace(store, GOLDEN)
    before = canonical_json(
        [exchange.model_dump() for exchange in store.exchanges(trace.id)]
    )
    store.close()
    plan = make_plan("llm_http_error", params={"status": 429})
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(canonical_json(plan.model_dump(by_alias=True)))
    main(
        [
            "eval",
            "faults",
            "--db",
            str(path),
            "--plan",
            str(plan_path),
            "--trace-id",
            trace.id,
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert (
        report["scored"] is False and report["execution"] == "fixed-recorded-trajectory"
    )
    result = report["runs"][0]
    assert len(result["faults"]) == 1 and result["exchanges"][0]["status_code"] == 429
    reopened = TraceStore(path)
    assert (
        canonical_json(
            [exchange.model_dump() for exchange in reopened.exchanges(trace.id)]
        )
        == before
    )
    run = reopened.get_run(result["run_id"])
    assert run.outcome == "completed" and run.fault_plan_id == plan.digest
    assert run.config["fault_evaluation"] == "fixed-recorded-trajectory"
    # M6 finalization now detects the observed HTTP error independently of its fault row.
    findings = reopened.findings(run.id)
    assert [finding.kind for finding in findings] == ["llm_error"]
    assert findings[0].exchange_seq == 0 and findings[0].evidence == {
        "status_code": 429
    }
    assert [finding["kind"] for finding in result["findings"]] == ["llm_error"]
    reopened.close()


def test_evaluator_exercises_tool_fault_and_reproducibility(store: TraceStore) -> None:
    trace = import_trace(store, GOLDEN)
    plan = make_plan(
        "tool_exec_error",
        target="tool_event",
        params={"exception": "ValueError", "message": "bad"},
    )
    results = [
        evaluate_faults(store, plan, clock=lambda: 123.0)["runs"][0] for _ in range(2)
    ]
    for result in results:
        assert result["tool_errors"] == [{"seq": 0, "error": "ValueError: bad"}]
        assert result["tools"][0]["error"] == "ValueError: bad"
        assert store.get_run(result["run_id"]).fault_plan_id == plan.digest
    normalized = [
        {
            key: value
            for key, value in result["faults"][0].items()
            if key not in {"id", "run_id"}
        }
        for result in results
    ]
    assert normalized[0] == normalized[1]
    assert store.tool_events(trace.id)[0].result == 5


@pytest.mark.parametrize("problem", ["empty", "bad_plan"])
def test_fault_eval_usage_errors_are_exit_two(tmp_path: Path, problem: str) -> None:
    plan = tmp_path / "plan.yaml"
    plan.write_text(
        "invalid"
        if problem == "bad_plan"
        else canonical_json(make_plan("llm_timeout").model_dump(by_alias=True))
    )
    with pytest.raises(SystemExit) as error:
        main(
            ["eval", "faults", "--db", str(tmp_path / "empty.db"), "--plan", str(plan)]
        )
    assert error.value.code == 2


def test_evaluator_finalizes_run_when_fault_storage_fails(
    store: TraceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentreplay.schema import Fault

    trace = import_trace(store, GOLDEN)

    def fail_storage(fault: Fault) -> None:
        raise OSError("fault storage failed")

    monkeypatch.setattr(store, "add_fault", fail_storage)
    with pytest.raises(OSError, match="storage failed"):
        evaluate_faults(store, make_plan("llm_http_error"))
    run = store.runs(trace.id)[0]
    assert run.outcome == "agent_error" and "fault storage failed" in run.detail
    assert store.faults(run.id) == []


@pytest.mark.parametrize("mode", ["record", "replay"])
def test_proxy_cli_fault_flag_loads_plan_before_starting_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    database = tmp_path / "serve.db"
    store = TraceStore(database)
    trace = import_trace(store, GOLDEN)
    store.close()
    plan = make_plan("llm_http_error")
    path = tmp_path / "fault.yaml"
    path.write_text(canonical_json(plan.model_dump(by_alias=True)))
    served = []

    def fake_serve(app: Any, host: str, port: int) -> None:
        run = app.state.runs.get_run(app.state.run_id)
        assert run.fault_plan_id == plan.digest
        assert run.config["fault_plan"] == plan.model_dump(by_alias=True)
        served.append(app)

    monkeypatch.setattr("agentreplay.cli.uvicorn.run", fake_serve)
    main([mode, "--db", str(database), "--trace-id", trace.id, "--faults", str(path)])
    assert len(served) == 1
