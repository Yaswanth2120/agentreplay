"""Complete offline evaluation, cold reports, error boundaries, and labels."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentreplay.cli import main
from agentreplay.evals.harness import evaluate, load_config, reconstruct_state, run_task
from agentreplay.evals.labeling import label_run
from agentreplay.evals.scripted import scripted_response
from agentreplay.evals.stats import summarize
from agentreplay.evals.tasks import load_tasks, tasks_version
from agentreplay.report import (
    build_report,
    load_records,
    observed_call_count,
    render_markdown,
    validate_cohort,
    validate_rates,
    write_report,
)
from agentreplay.schema import (
    EvaluationConfig,
    EvaluationResult,
    Exchange,
    Run,
    RunObservations,
    Trace,
)
from agentreplay.store import TraceStore


@pytest.fixture(scope="module")
def full_evaluation(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, str, dict[str, Any]]:
    database = tmp_path_factory.mktemp("evaluation") / "offline.db"
    config = load_config("examples/eval-offline.yaml")
    store = TraceStore(database)
    try:
        result = asyncio.run(
            evaluate(store, config, clock=lambda: 123.0, git_sha="test-sha")
        )
        report = write_report(
            store, database.parent / "reports", result["evaluation_id"]
        )
        assert len(result["run_ids"]) == 480
        return database, result["evaluation_id"], report
    finally:
        store.close()


def test_twenty_tasks_three_trials_complete_report_and_cold_reload(
    full_evaluation: tuple[Path, str, dict[str, Any]],
) -> None:
    database, evaluation_id, report = full_evaluation
    metrics = report["metrics"]
    assert metrics["n_runs"] == 480
    assert metrics["versions"]["v1"]["task_success"]["successes"] == 60
    assert metrics["versions"]["v1"]["task_success"]["n"] == 60
    assert metrics["versions"]["v2"]["task_success"]["successes"] == 33
    assert metrics["replay_determinism"]["value"] == 1
    assert metrics["replay_determinism"]["n"] == 120
    assert metrics["clean_false_positives"]["any"]["successes"] == 0
    assert metrics["clean_false_positives"]["any"]["n"] == 186
    assert metrics["regression_divergence"]["successes"] == 27
    assert metrics["regression_divergence"]["n"] == 60
    assert metrics["injected"]["per_kind"]["llm_error"]["tp"] == 60
    assert metrics["injected"]["per_kind"]["llm_error"]["fn"] == 0
    assert metrics["injected"]["per_kind"]["llm_error"]["fp"] == 0
    assert metrics["paired"]["success_difference"]["value"] == -27 / 60
    assert metrics["paired"]["success_difference"]["resamples"] == 10000
    assert report["config"]["tasks_version"] == tasks_version()
    assert report["config"]["git_sha"] == "test-sha"
    assert report["config"]["backend"] == "scripted"
    assert report["config"]["owner_review"] == "pending"
    store = TraceStore(database)
    try:
        assert build_report(store, evaluation_id) == build_report(store) == report
        for run in store.all_runs():
            assert (
                run.finished_at == 123
                and run.task_id
                and run.agent_version
                and run.trial is not None
            )
            assert run.config["sampling"] == {"temperature": 0.7, "seed": run.trial}
            assert run.config["vllm_version"] == "not-used-scripted"
            assert run.config["vllm_flags"] == []
            assert store.get_evaluation(run.id) is not None
            if run.config["phase"] == "record":
                assert store.faults(run.id) == []
                assert all(
                    exchange.status_code == 200
                    for exchange in store.exchanges(run.trace_id)
                )
                assert (
                    store.tool_events(run.trace_id)
                    == store.get_observations(run.id).tool_events
                )
                if run.agent_version == "v1":
                    assert all(
                        event.error is None for event in store.tool_events(run.trace_id)
                    )
            if run.task_success is False:
                assert (
                    sum(f.kind == "task_failure" for f in store.findings(run.id)) == 1
                )
        before = (database.parent / "reports" / "summary.json").read_bytes()
        write_report(store, database.parent / "reports", evaluation_id)
        assert (database.parent / "reports" / "summary.json").read_bytes() == before
    finally:
        store.close()
    markdown = (database.parent / "reports" / "summary.md").read_text()
    assert markdown.startswith("```json\n")
    assert "n |" in markdown and "undefined" in markdown
    assert "not live model capability" in markdown and "pending" in markdown


def test_reconstruction_uses_only_consumed_actions_and_handles_missing_prerequisites(
    full_evaluation: tuple[Path, str, dict[str, Any]],
) -> None:
    database, _, _ = full_evaluation
    store = TraceStore(database)
    try:
        sources = [r for r in store.all_runs() if r.config["phase"] == "record"]
        source = next(
            r for r in sources if r.task_id == "files-017" and r.agent_version == "v1"
        )
        task = next(t for t in load_tasks() if t.id == source.task_id)
        state, errors = reconstruct_state(store, source, [0], task)
        assert errors == [] and set(state) == {"notes/readme.txt", "reports/part-0.txt"}
        assert state != store.get_evaluation(source.id).final_state
        ticket = next(
            r for r in sources if r.task_id == "tickets-017" and r.agent_version == "v1"
        )
        ticket_task = next(t for t in load_tasks() if t.id == ticket.task_id)
        state, errors = reconstruct_state(store, ticket, [1, 2], ticket_task)
        assert errors and len(state) == 1
    finally:
        store.close()


def test_reports_reject_missing_denominators_invalid_values_and_partial_cohorts(
    full_evaluation: tuple[Path, str, dict[str, Any]],
) -> None:
    database, id, report = full_evaluation
    with pytest.raises(ValueError, match="without its n"):
        validate_rates({"rate": {"value": 0.5}})
    for value in [
        {"value": 0.1, "n": 0},
        {"value": float("nan"), "n": 3},
        {"value": 0.5, "n": True},
    ]:
        with pytest.raises(ValueError):
            validate_rates(value)
    damaged = copy.deepcopy(report)
    del damaged["metrics"]["versions"]["v1"]["task_success"]["n"]
    with pytest.raises(ValueError, match="without its n"):
        render_markdown(damaged)
    store = TraceStore(database)
    try:
        records = load_records(store, id)
    finally:
        store.close()
    cases = [
        records[1:],
        records + [records[0]],
        [r for r in records if r["run"]["config"]["phase"] != "fault_replay"],
        [r for r in records if r["run"]["config"]["repeat"] != 1],
    ]
    for partial in cases:
        with pytest.raises(ValueError):
            validate_cohort(partial)
    wrong = copy.deepcopy(records)
    wrong[-1]["run"]["config"]["git_sha"] = "different"
    with pytest.raises(ValueError, match="metadata"):
        validate_cohort(wrong)
    wrong = copy.deepcopy(records)
    replay = next(r for r in wrong if r["run"]["config"]["phase"] == "clean_replay")
    replay["run"]["config"]["clean_source"] = not replay["run"]["config"][
        "clean_source"
    ]
    with pytest.raises(ValueError, match="eligibility"):
        validate_cohort(wrong)
    partial = [
        r
        for r in records
        if not (
            r["run"]["config"]["phase"] == "clean_replay"
            and r["run"]["config"]["repeat"] == 1
        )
    ]
    with pytest.raises(ValueError, match="two replay"):
        summarize(partial, records[0]["run"]["config"]["evaluation_config"])


def test_statistics_catch_non_determinism_and_false_positives(
    full_evaluation: tuple[Path, str, dict[str, Any]],
) -> None:
    database, id, _ = full_evaluation
    store = TraceStore(database)
    try:
        records = load_records(store, id)
    finally:
        store.close()
    replay = next(
        r
        for r in records
        if r["run"]["config"]["phase"] == "clean_replay"
        and r["run"]["config"]["clean_source"]
    )
    replay["findings"].append(
        {
            "kind": "timeout",
            "severity": "error",
            "exchange_seq": 0,
            "tool_seq": None,
            "evidence": {},
        }
    )
    metrics = summarize(records, records[0]["run"]["config"]["evaluation_config"])
    assert metrics["replay_determinism"]["successes"] == 119
    assert metrics["clean_false_positives"]["any"]["successes"] == 1
    assert metrics["clean_false_positives"]["per_kind"]["timeout"]["successes"] == 1


def test_evaluation_atomic_rollback_and_persisted_results(store: TraceStore) -> None:
    trace = store.create_trace(Trace(name="atomic"))
    run = store.create_run(Run(kind="record", trace_id=trace.id))
    with pytest.raises(ValueError, match="evaluation"):
        store.finish_run(
            run.id,
            "completed",
            finished_at=1,
            observations=RunObservations(),
            evaluation=EvaluationResult(
                run_id="wrong",
                final_answer="x",
                final_state={},
                state_source="executed",
            ),
        )
    assert store.get_run(run.id).finished_at is None
    assert store.get_observations(run.id) is None
    assert store.get_evaluation(run.id) is None
    result = EvaluationResult(
        run_id=run.id,
        final_answer="x",
        final_state={"key": [1]},
        state_source="executed",
    )
    store.finish_run(run.id, "completed", finished_at=2, evaluation=result)
    assert store.get_evaluation(run.id) == result
    assert store.all_runs() == [store.get_run(run.id)]
    assert store.get_evaluation("missing") is None


def test_manual_labeling_is_blind_transactional_repeatable_and_natural_only(
    store: TraceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = store.create_trace(Trace(name="label"))
    run = store.create_run(Run(kind="record", trace_id=trace.id))
    with pytest.raises(ValueError, match="finished"):
        label_run(store, run.id)
    store.finish_run(run.id, "completed", finished_at=1)

    def forbidden(*args: Any) -> None:
        pytest.fail("labeling inspected detector output")

    monkeypatch.setattr(store, "findings", forbidden)
    answers = iter(["y", "n", "y"])
    labels = label_run(
        store, run.id, ask=lambda prompt: next(answers), clock=lambda: 2.0
    )
    assert len(labels) == 3 and len(store.labels(run.id)) == 3
    assert all(label.created_at == 2 for label in labels)
    answers = iter(["n", "n", "n"])
    label_run(store, run.id, ask=lambda prompt: next(answers), clock=lambda: 3.0)
    assert all(not label.present for label in store.labels(run.id))
    answers = iter(["y", "invalid"])
    with pytest.raises(ValueError, match="no labels saved"):
        label_run(store, run.id, ask=lambda prompt: next(answers))
    assert all(not label.present for label in store.labels(run.id))
    before = store.labels(run.id)
    with pytest.raises(ValueError, match="duplicate"):
        store.replace_labels([before[0], before[0]])
    assert store.labels(run.id) == before
    invalid = before[0].model_copy(update={"run_id": "missing"})
    with pytest.raises(sqlite3.IntegrityError):
        store.replace_labels([before[0], invalid])
    assert store.labels(run.id) == before
    injected = store.create_run(
        Run(kind="replay", trace_id=trace.id, fault_plan_id="a-plan")
    )
    store.finish_run(injected.id, "completed", finished_at=1)
    with pytest.raises(ValueError, match="natural"):
        label_run(store, injected.id)
    with pytest.raises(ValueError):
        label_run(store, "missing")


@pytest.mark.parametrize(
    "config",
    [
        {"trials": 2},
        {"versions": []},
        {"versions": ["v1", "v1"]},
        {"task_ids": []},
        {"task_ids": ["x", "x"]},
        {"backend": "live"},
        {"unknown": 1},
        {"versions": ["v2"], "fault_plan": {}},
    ],
)
def test_invalid_evaluation_config_rejected(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        EvaluationConfig.model_validate(config)


def test_unknown_tasks_and_bad_plans_fail_before_creating_runs(
    store: TraceStore,
) -> None:
    for config in [
        EvaluationConfig(task_ids=["unknown"]),
        EvaluationConfig(task_ids=["calc-001"], fault_plan={"version": 99}),
    ]:
        with pytest.raises(ValueError):
            asyncio.run(evaluate(store, config))
    assert store.all_runs() == [] and store.list_traces() == []
    with pytest.raises(ValueError, match="no evaluation"):
        build_report(store)


def test_agent_and_infrastructure_failures_finalize_without_unearned_success(
    store: TraceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = EvaluationConfig(
        task_ids=["calc-001"], versions=["v1"], bootstrap_resamples=100
    )
    task = load_tasks()[0]

    def error_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    async def exercise() -> None:
        run = await run_task(
            store,
            task,
            0,
            "v1",
            config,
            {},
            transport=httpx.MockTransport(error_response),
            clock=lambda: 2,
        )
        assert run.outcome == "agent_error" and run.task_success is False
        assert {f.kind for f in store.findings(run.id)} == {"llm_error", "task_failure"}

    asyncio.run(exercise())
    from agentreplay.evals import harness

    def fail_verifier(*args: Any) -> bool:
        raise RuntimeError("verification infrastructure failed")

    monkeypatch.setattr(harness, "verify", fail_verifier)
    with pytest.raises(RuntimeError, match="verification infrastructure"):
        asyncio.run(
            run_task(
                store,
                task,
                0,
                "v1",
                config,
                {},
                transport=httpx.MockTransport(scripted_response),
            )
        )
    broken = store.all_runs()[-1]
    assert broken.finished_at is not None and broken.task_success is None
    assert store.get_evaluation(broken.id) is None
    assert "infrastructure failure" in broken.detail


def test_cli_eval_run_report_and_label(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, config_path, output = (
        tmp_path / "cli.db",
        tmp_path / "eval.yaml",
        tmp_path / "report",
    )
    config_path.write_text(
        "task_ids: [calc-001]\nversions: [v1]\ntrials: 3\nbootstrap_resamples: 100\n"
    )
    main(["eval", "run", "--db", str(database), "--config", str(config_path)])
    result = json.loads(capsys.readouterr().out)
    assert len(result["run_ids"]) == 9
    main(["report", "--db", str(database), "--out", str(output)])
    report = json.loads(capsys.readouterr().out)
    assert report == json.loads((output / "summary.json").read_text())
    assert report["metrics"]["versions"]["v1"]["task_success"]["n"] == 3
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    main(["label", "--db", str(database), "--run-id", result["run_ids"][0]])
    assert len(json.loads(capsys.readouterr().out)) == 3
    with pytest.raises(SystemExit) as error:
        main(
            [
                "report",
                "--db",
                str(database),
                "--evaluation-id",
                "unknown",
                "--out",
                str(output),
            ]
        )
    assert error.value.code == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("invalid: [")
    with pytest.raises(ValueError, match="YAML"):
        load_config(bad)
    with pytest.raises(SystemExit) as error:
        main(["eval", "run", "--db", str(database), "--config", str(bad)])
    assert error.value.code == 2


def test_observed_call_count_handles_malformed_containers() -> None:
    def exchange(response: dict[str, Any] | None) -> Exchange:
        return Exchange.from_request("trace", 0, {}, response=response, status_code=200)

    assert (
        observed_call_count(
            [
                exchange(None),
                exchange({"choices": "bad"}),
                exchange({"choices": [None, {"message": {"tool_calls": "bad"}}]}),
            ]
        )
        == 1
    )
    assert (
        observed_call_count(
            [exchange({"choices": [{"message": {"tool_calls": [{}, {}]}}]})]
        )
        == 2
    )


@pytest.mark.parametrize(
    ("kind", "target", "expected"),
    [
        ("llm_timeout", "exchange", "timeout"),
        ("llm_http_error", "exchange", "llm_error"),
        ("llm_malformed_json", "exchange", "malformed_response"),
        ("llm_empty_response", "exchange", "malformed_response"),
        ("tool_args_corrupt", "exchange", "invalid_tool_call"),
        ("tool_wrong_name", "exchange", "invalid_tool_call"),
        ("tool_args_schema_violation", "exchange", "invalid_tool_call"),
        ("tool_loop", "exchange", "loop"),
        ("tool_loop", "tool_event", "loop"),
        ("tool_exec_timeout", "tool_event", "tool_error"),
        ("tool_exec_error", "tool_event", "tool_error"),
        ("tool_bad_result", "tool_event", "task_failure"),
    ],
)
def test_adaptive_fault_replays_produce_observed_failures_without_mutating_source(
    store: TraceStore, kind: str, target: str, expected: str
) -> None:
    from test_faults import make_plan

    from agentreplay.evals.stats import injection_scores

    task = next(t for t in load_tasks() if t.id == "calc-017")
    config = EvaluationConfig(
        versions=["v1"], task_ids=[task.id], bootstrap_resamples=100
    )
    params = (
        {"schema": {"type": "number"}, "value": "bad"}
        if kind == "tool_bad_result"
        else None
    )
    plan = make_plan(kind, target=target, params=params)

    async def exercise() -> None:
        source = await run_task(store, task, 0, "v1", config, {}, clock=lambda: 2)
        exchanges = store.exchanges(source.trace_id)
        tools = store.tool_events(source.trace_id)
        replay = await run_task(
            store,
            task,
            0,
            "v1",
            config,
            {},
            source=source,
            phase="fault_replay",
            plan=plan,
            clock=lambda: 3,
        )
        findings = store.findings(replay.id)
        assert any(f.kind == expected for f in findings)
        assert replay.task_success is False
        assert store.faults(replay.id)
        assert store.exchanges(source.trace_id) == exchanges
        assert store.tool_events(source.trace_id) == tools
        score = injection_scores(
            [
                {
                    "faults": [f.model_dump() for f in store.faults(replay.id)],
                    "findings": [f.model_dump() for f in findings],
                }
            ]
        )
        if expected == "task_failure":
            assert score["unscored_faults"] == 1
        else:
            assert score["per_kind"][expected]["tp"] == 1

    asyncio.run(exercise())


def test_unfinished_evaluation_is_not_reported_as_completed(store: TraceStore) -> None:
    trace = store.create_trace(Trace(name="unfinished"))
    store.create_run(
        Run(kind="record", trace_id=trace.id, config={"evaluation_id": "incomplete"})
    )
    with pytest.raises(ValueError, match="incomplete"):
        build_report(store)


def test_manual_labels_from_database_remain_separate_from_injections(
    full_evaluation: tuple[Path, str, dict[str, Any]], tmp_path: Path
) -> None:
    import shutil

    from agentreplay.schema import EvalLabel

    path = tmp_path / "manual.db"
    shutil.copy2(full_evaluation[0], path)
    store = TraceStore(path)
    try:
        run = next(
            r
            for r in store.all_runs()
            if r.config["phase"] == "record" and r.agent_version == "v1"
        )
        store.replace_labels(
            [
                EvalLabel(
                    run_id=run.id,
                    kind="loop",
                    present=True,
                    labeler="reviewer",
                    created_at=1.0,
                )
            ]
        )
        report = build_report(store)
        assert report["metrics"]["manual"]["per_kind"]["loop"]["fn"] == 1
        assert report["metrics"]["manual"]["per_kind"]["loop"]["n_labeled"] == 1
        assert report["metrics"]["injected"]["per_kind"]["llm_error"]["tp"] == 60
    finally:
        store.close()


def test_live_path_uses_injected_upstream_without_opening_a_socket(
    store: TraceStore,
) -> None:
    config = EvaluationConfig(
        backend="live",
        model="fake-fixture-model",
        vllm_version="fake-fixture-version",
        vllm_flags=["--test-fixture"],
        versions=["v1"],
        task_ids=["calc-001"],
        bootstrap_resamples=100,
    )
    requests = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return scripted_response(request)

    asyncio.run(evaluate(store, config, transport=httpx.MockTransport(upstream)))
    sources = [r for r in store.all_runs() if r.config["phase"] == "record"]
    assert len(sources) == 3 and all(r.kind == "live" for r in sources)
    assert len(requests) == 6
    report = build_report(store)
    assert report["config"]["vllm_version"] == "fake-fixture-version"
    assert report["config"]["vllm_flags"] == ["--test-fixture"]
    assert report["config"]["backend"] == "live"
    assert report["metrics"]["replay_determinism"]["value"] == 1


def test_cancelled_agent_is_finalized_as_aborted(
    store: TraceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentreplay.evals import harness

    async def cancelled(*args: Any, **kwargs: Any) -> str:
        raise asyncio.CancelledError

    monkeypatch.setattr(harness, "run_agent", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            evaluate(
                store,
                EvaluationConfig(
                    versions=["v1"], task_ids=["calc-001"], bootstrap_resamples=100
                ),
            )
        )
    run = store.all_runs()[0]
    assert (
        run.outcome == "aborted"
        and run.finished_at is not None
        and run.task_success is None
    )
    with pytest.raises(ValueError, match="incomplete"):
        build_report(store)


def test_git_revision_is_explicit_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from agentreplay.evals import harness

    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="abc123\n"),
    )
    assert harness.git_revision() == "abc123"
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert harness.git_revision() == "unknown"

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(harness.subprocess, "run", unavailable)
    assert harness.git_revision() == "unknown"


def test_reconstructed_side_effects_survive_post_execution_tool_fault(
    store: TraceStore,
) -> None:
    from test_faults import make_plan

    task = next(t for t in load_tasks() if t.id == "kv-001")
    config = EvaluationConfig(
        versions=["v1"], task_ids=[task.id], bootstrap_resamples=100
    )

    async def exercise() -> None:
        source = await run_task(store, task, 0, "v1", config, {})
        replay = await run_task(
            store,
            task,
            0,
            "v1",
            config,
            {},
            source=source,
            phase="fault_replay",
            plan=make_plan("tool_exec_error", target="tool_event"),
        )
        assert replay.outcome == "agent_error" and replay.task_success is True
        assert (
            store.get_evaluation(replay.id).final_state
            == store.get_evaluation(source.id).final_state
        )
        kinds = {finding.kind for finding in store.findings(replay.id)}
        assert "tool_error" in kinds and "task_failure" not in kinds

    asyncio.run(exercise())
