"""Offline follow-up on a copy of a measured cohort; never rewrite live results."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from agentreplay.evals.harness import git_revision, run_task
from agentreplay.evals.stats import injection_scores, rate
from agentreplay.faults import FaultPlan
from agentreplay.schema import EvaluationConfig, Run, Task
from agentreplay.store import TraceStore


async def verify_sources(
    store: TraceStore, sources: list[Run], *, git_sha: str
) -> dict[str, Any]:
    if not sources:
        raise ValueError("source cohort contains no measured recordings")
    evaluation_id = uuid.uuid4().hex
    faithful = 0
    failures = []
    loop_records = []
    replay_ids = []
    for source in sources:
        config = EvaluationConfig.model_validate(source.config["evaluation_config"])
        task = Task.model_validate(source.config["task"])
        metadata = {
            "evaluation_id": evaluation_id,
            "git_sha": git_sha,
            "source_evaluation_id": source.config["evaluation_id"],
            "source_git_sha": source.config["git_sha"],
            "verification_only": True,
        }
        replay = await run_task(
            store,
            task,
            source.trial,
            source.agent_version,
            config,
            metadata,
            phase="clean_replay",
            source=source,
            repeat=0,
        )
        replay_ids.append(replay.id)
        original = store.get_evaluation(source.id)
        result = store.get_evaluation(replay.id)
        divergences = [
            f.kind
            for f in store.findings(replay.id)
            if f.kind.startswith(("replay_", "tool_replay_"))
        ]
        same = (
            original is not None
            and result is not None
            and not divergences
            and replay.outcome == source.outcome
            and replay.task_success == source.task_success
            and result.final_answer == original.final_answer
            and result.final_state == original.final_state
            and result.state_errors == original.state_errors
        )
        faithful += int(same)
        if not same:
            failures.append(
                {
                    "source_run_id": source.id,
                    "replay_run_id": replay.id,
                    "divergences": divergences,
                }
            )
        if (
            source.agent_version != "v1"
            or not source.task_success
            or store.findings(source.id)
        ):
            continue
        values = (
            [config.fault_plan] if config.fault_plan is not None else config.fault_plans
        )
        for value in values:
            plan = FaultPlan.from_dict(value)
            if not plan.faults or any(
                rule.fault_class != "tool_loop" for rule in plan.faults
            ):
                continue
            run = await run_task(
                store,
                task,
                source.trial,
                source.agent_version,
                config,
                metadata,
                phase="fault_replay",
                source=source,
                plan=plan,
            )
            loop_records.append(
                {
                    "run_id": run.id,
                    "faults": [fault.model_dump() for fault in store.faults(run.id)],
                    "findings": [
                        finding.model_dump() for finding in store.findings(run.id)
                    ],
                }
            )
    return {
        "evaluation_id": evaluation_id,
        "git_sha": git_sha,
        "source_evaluation_id": sources[0].config["evaluation_id"],
        "source_git_shas": sorted({run.config["git_sha"] for run in sources}),
        "source_fidelity": rate(faithful, len(sources)),
        "failures": failures,
        "replay_run_ids": replay_ids,
        "loop_injections": injection_scores(loop_records),
        "loop_run_ids": [record["run_id"] for record in loop_records],
        "notes": [
            "Post-fix offline follow-up on archived traces; not a new live evaluation.",
            "Source fidelity requires equal outcomes, task success, answers, states, state errors, and no replay divergence.",
            "Loop runs reuse the original cohort's loop plans on successful finding-free v1 sources.",
            "These are post-hoc development checks, not independent validation or new model performance measurements.",
            "The original live report and source cohort remain unchanged; owner review and blind labels remain pending.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--source-evaluation-id", required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.source_db.is_file():
        parser.error("source database does not exist")
    if args.db.exists() or args.out.exists():
        parser.error("use new database and output paths; verification never overwrites")
    source_store = TraceStore(args.source_db)
    try:
        sources = [
            run
            for run in source_store.all_runs()
            if run.config.get("evaluation_id") == args.source_evaluation_id
            and run.config.get("phase") == "record"
        ]
        if not sources or any(
            source_store.get_evaluation(run.id) is None or run.finished_at is None
            for run in sources
        ):
            parser.error("source cohort needs measured, finished recordings")
        source_store.backup(args.db)
    finally:
        source_store.close()
    store = TraceStore(args.db)
    try:
        report = asyncio.run(verify_sources(store, sources, git_sha=git_revision()))
    finally:
        store.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    loop = report["loop_injections"]["per_kind"]["loop"]
    return int(bool(report["failures"] or loop["fp"] or loop["fn"]))


if __name__ == "__main__":
    raise SystemExit(main())
