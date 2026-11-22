from __future__ import annotations

import argparse
import json
import sqlite3
import sys

import uvicorn

from .checks import check_golden
from .detection import detect_run
from .evals.faults import evaluate_faults
from .evals.harness import load_config, run_evaluation
from .evals.labeling import blind_run, label_run
from .exportio import export_trace, import_trace
from .faults import FaultPlan
from .inspection import render_list, render_run_diff, render_trace
from .proxy import create_app
from .report import write_report
from .store import TraceStore


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentreplay")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("record", "replay"):
        command = sub.add_parser(name)
        command.add_argument("--db", default="traces.db")
        command.add_argument("--port", type=int, default=9000)
        command.add_argument("--faults")
        command.add_argument("--trace-id", default=None, required=(name == "replay"))
        if name == "record":
            command.add_argument("--name", default="unnamed")
            command.add_argument("--upstream", default="http://localhost:8000")
        else:
            command.add_argument(
                "--mode", choices=["strict", "lenient"], default="strict"
            )
    for name in ("list", "show", "export", "import"):
        command = sub.add_parser(name)
        command.add_argument("--db", default="traces.db")
        if name in {"show", "export"}:
            command.add_argument("--trace-id", required=True)
        if name == "export":
            command.add_argument("--out", required=True)
        if name == "import":
            command.add_argument("--in", dest="input_path", required=True)
    command = sub.add_parser("diff")
    command.add_argument("--db", default="traces.db")
    command.add_argument("--run-a", required=True)
    command.add_argument("--run-b", required=True)
    command = sub.add_parser("check")
    command.add_argument("--golden", required=True)
    command.add_argument("--agent", required=True)
    command.add_argument("--mode", choices=["strict", "lenient"], default="strict")
    command = sub.add_parser("detect")
    command.add_argument("--db", default="traces.db")
    command.add_argument("--run-id", required=True)
    evaluation = sub.add_parser("eval")
    evaluation_sub = evaluation.add_subparsers(dest="eval_cmd", required=True)
    command = evaluation_sub.add_parser("faults")
    command.add_argument("--db", default="traces.db")
    command.add_argument("--plan", required=True)
    command.add_argument("--trace-id")
    command = evaluation_sub.add_parser("run")
    command.add_argument("--db", default="traces.db")
    command.add_argument("--config", required=True)
    command.add_argument("--resume", help="Resume an existing evaluation ID")
    command.add_argument(
        "--replay-only",
        action="store_true",
        help="Resume only after every source pair is measured; never call a model",
    )
    command = sub.add_parser("report")
    command.add_argument("--db", default="traces.db")
    command.add_argument("--out", default="reports")
    command.add_argument("--evaluation-id")
    command = sub.add_parser("label")
    command.add_argument("--db", default="traces.db")
    command.add_argument("--run-id", required=True)
    command.add_argument("--labeler", default="owner")
    args = parser.parse_args(argv)
    store = None
    try:
        if args.cmd == "check":
            result = check_golden(args.golden, args.agent, args.mode)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
            if not result["passed"]:
                raise SystemExit(1)
            return
        store = TraceStore(args.db)
        if args.cmd == "detect":
            print(
                json.dumps(
                    [
                        finding.model_dump()
                        for finding in detect_run(store, args.run_id)
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
            )
        elif args.cmd == "report":
            result = write_report(store, args.out, args.evaluation_id)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        elif args.cmd == "label":
            print(
                json.dumps(blind_run(store, args.run_id), ensure_ascii=False, indent=2),
                file=sys.stderr,
            )
            labels = label_run(store, args.run_id, labeler=args.labeler, ask=input)
            print(
                json.dumps(
                    [label.model_dump() for label in labels], sort_keys=True, indent=2
                )
            )
        elif args.cmd == "eval":
            result = (
                run_evaluation(
                    store, load_config(args.config), args.resume, args.replay_only
                )
                if args.eval_cmd == "run"
                else evaluate_faults(store, FaultPlan.load(args.plan), args.trace_id)
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        elif args.cmd == "list":
            print(render_list(store))
        elif args.cmd == "diff":
            print(render_run_diff(store, args.run_a, args.run_b))
        elif args.cmd == "show":
            print(render_trace(store, args.trace_id))
        elif args.cmd == "export":
            export_trace(store, args.trace_id, args.out)
            print(f"Exported {args.trace_id} to {args.out}")
        elif args.cmd == "import":
            trace = import_trace(store, args.input_path)
            print(f"Imported {trace.id}: {trace.name}")
        else:
            app = (
                create_app(
                    store,
                    "record",
                    args.trace_id,
                    args.name,
                    args.upstream,
                    faults=FaultPlan.load(args.faults) if args.faults else None,
                )
                if args.cmd == "record"
                else create_app(
                    store,
                    "replay",
                    args.trace_id,
                    replay_mode=args.mode,
                    faults=FaultPlan.load(args.faults) if args.faults else None,
                )
            )
            print(f"trace_id={app.state.trace_id}")
            uvicorn.run(app, host="127.0.0.1", port=args.port)
    except (ValueError, TypeError, OSError, sqlite3.Error) as error:
        parser.error(str(error))
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    main()
