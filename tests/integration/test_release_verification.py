"""Release follow-up executes only archived responses in a separate database."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agentreplay.evals.harness import evaluate
from agentreplay.schema import EvaluationConfig
from agentreplay.store import TraceStore
from scripts.verify_release import main, verify_sources


def test_release_followup_preserves_source_and_localizes_batched_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.db"
    config = EvaluationConfig(
        task_ids=["kv-017"],
        fault_plans=[
            {
                "version": 1,
                "seed": 1234,
                "faults": [
                    {
                        "class": "tool_loop",
                        "target": target,
                        "select": {"seq": [0]},
                        "params": {"repeat": 5},
                    }
                ],
            }
            for target in ["exchange", "tool_event"]
        ],
    )
    source = TraceStore(source_path)
    cohort = asyncio.run(evaluate(source, config, git_sha="recording-sha"))
    before = [run.model_dump() for run in source.all_runs()]
    source.close()
    out = tmp_path / "verification.json"
    db = tmp_path / "verification.db"
    arguments = [
        "verify_release",
        "--source-db",
        str(source_path),
        "--source-evaluation-id",
        cohort["evaluation_id"],
        "--db",
        str(db),
        "--out",
        str(out),
    ]
    monkeypatch.setattr(sys, "argv", arguments)
    assert main() == 0
    result = json.loads(out.read_text())
    assert result["source_fidelity"]["successes"] == result["source_fidelity"]["n"] == 6
    assert result["failures"] == []
    loop = result["loop_injections"]["per_kind"]["loop"]
    assert (loop["tp"], loop["fp"], loop["fn"]) == (6, 0, 0)
    assert result["source_git_shas"] == ["recording-sha"]
    source = TraceStore(source_path)
    try:
        assert [run.model_dump() for run in source.all_runs()] == before
    finally:
        source.close()
    saved = out.read_bytes()
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and out.read_bytes() == saved

    from agentreplay.evals import agent

    monkeypatch.setattr(agent, "SYSTEM", agent.SYSTEM + " Changed agent.")
    changed_out = tmp_path / "changed.json"
    changed_args = arguments.copy()
    changed_args[changed_args.index("--db") + 1] = str(tmp_path / "changed.db")
    changed_args[changed_args.index("--out") + 1] = str(changed_out)
    monkeypatch.setattr(sys, "argv", changed_args)
    assert main() == 1
    changed = json.loads(changed_out.read_text())
    assert changed["source_fidelity"]["successes"] == 0
    assert len(changed["failures"]) == 6
    assert all("replay_miss" in row["divergences"] for row in changed["failures"])


def test_release_followup_rejects_missing_cohort_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    store = TraceStore(source)
    try:
        with pytest.raises(ValueError, match="no measured recordings"):
            asyncio.run(verify_sources(store, [], git_sha="test"))
    finally:
        store.close()
    target = tmp_path / "target.db"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_release",
            "--source-db",
            str(source),
            "--source-evaluation-id",
            "missing",
            "--db",
            str(target),
            "--out",
            str(tmp_path / "out.json"),
        ],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2 and not target.exists()
