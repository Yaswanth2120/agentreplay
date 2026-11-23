"""AI evidence stays auditable and never becomes human ground truth."""

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

from agentreplay.evals.labeling import blind_run
from agentreplay.report import render_markdown
from agentreplay.schema import Exchange, Run, Trace
from agentreplay.store import TraceStore
from scripts import finalize_ai_review as review

ROOT = Path(__file__).parents[2]


@pytest.fixture
def ai_evidence(store: TraceStore, tmp_path: Path):
    sample = json.loads((ROOT / "reports/live/manual-sample.json").read_text())
    evidence = json.loads((ROOT / "reviews/ai-natural-labels.json").read_text())
    trace = store.create_trace(Trace(name="isolated review test"))
    store.add_exchange(
        Exchange.from_request(
            trace.id,
            0,
            {"messages": [{"role": "user", "content": "test"}]},
            status_code=200,
            response={"choices": [{"message": {"content": "done"}}]},
        )
    )
    for row in evidence["rows"]:
        run = store.create_run(
            Run(
                id=row["run_id"],
                kind="live",
                trace_id=trace.id,
                task_id=row["task_id"],
                trial=row["trial"],
                config={
                    "phase": "record",
                    "evaluation_id": evidence["source_evaluation_id"],
                },
            )
        )
        store.finish_run(run.id, "completed", finished_at=1.0)
        transcript = json.dumps(blind_run(store, run.id), indent=2) + "\n"
        row["blind_transcript_sha256"] = hashlib.sha256(transcript.encode()).hexdigest()
    sample_path = tmp_path / "sample.json"
    sample_path.write_text(json.dumps(sample))
    evidence["sample_sha256"] = review.file_sha(sample_path)
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps(evidence))
    return labels, sample_path, evidence


def test_task_review_checks_reasoned_solutions_against_frozen_goals(
    tmp_path: Path,
) -> None:
    path = ROOT / "reviews/ai-task-solutions.json"
    checked = review.check_task_review(path)
    assert checked["verified_solutions"]["successes"] == checked["n"] == 20
    evidence = json.loads(path.read_text())
    evidence["rows"][0]["final_answer"] = '{"result": -999}'
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="adjudication"):
        review.check_task_review(wrong)
    evidence["tasks_version_sha256"] = "old tasks"
    wrong.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="different frozen task"):
        review.check_task_review(wrong)


@pytest.mark.parametrize(
    "tamper", ["human", "missing", "duplicate", "hash", "boolean", "task"]
)
def test_ai_labels_reject_untrusted_or_incomplete_evidence(
    store: TraceStore, ai_evidence, tamper: str
) -> None:
    labels, sample, evidence = ai_evidence
    if tamper == "human":
        evidence["review_type"] = "human_review"
    elif tamper == "missing":
        evidence["rows"].pop()
    elif tamper == "duplicate":
        evidence["rows"][-1] = evidence["rows"][0]
    elif tamper == "hash":
        evidence["rows"][0]["blind_transcript_sha256"] = "changed input"
    elif tamper == "boolean":
        evidence["rows"][0]["labels"]["loop"] = "false"
    else:
        evidence["rows"][0]["task_id"] = "wrong-task"
    labels.write_text(json.dumps(evidence))
    with pytest.raises(ValueError):
        review.check_label_review(store, labels, sample)


def test_ai_report_separates_human_metrics_and_preserves_historical_status(
    store: TraceStore, ai_evidence, monkeypatch: pytest.MonkeyPatch
) -> None:
    labels, sample, evidence = ai_evidence
    original = json.loads((ROOT / "reports/offline/summary.json").read_text())
    monkeypatch.setattr(review, "build_report", lambda *args: copy.deepcopy(original))
    report = review.release_report(
        store, ROOT / "reviews/ai-task-solutions.json", labels, sample
    )
    assert report["metrics"]["manual"] == original["metrics"]["manual"]
    assert (
        report["config"]["owner_review"]
        == original["config"]["owner_review"]
        == "pending"
    )
    assert report["config"]["release_review"]["human_validation"] == "not_performed"
    for kind, counts in report["metrics"]["ai_assisted"]["per_kind"].items():
        assert counts["n_labeled"] == counts["tn"] == 100
        assert counts["precision"]["value"] is counts["recall"]["value"] is None
        assert f"| ai_assisted | {kind} | 0 | 0 | 0 | 100 | 100 |" in render_markdown(
            report
        )
    assert all(store.labels(row["run_id"]) == [] for row in evidence["rows"])


def test_ai_report_command_preserves_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "source.db"
    db.write_text("")
    out = tmp_path / "out"
    out.mkdir()
    marker = out / "summary.json"
    marker.write_text("original")
    monkeypatch.setattr(sys, "argv", ["review", "--db", str(db), "--out", str(out)])
    with pytest.raises(SystemExit) as error:
        review.main()
    assert error.value.code == 2 and marker.read_text() == "original"
