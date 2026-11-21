from __future__ import annotations

import argparse
import sqlite3

import uvicorn

from .exportio import export_trace, import_trace
from .inspection import render_list, render_trace
from .proxy import create_app
from .store import TraceStore


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentreplay")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("record", "replay"):
        command = sub.add_parser(name)
        command.add_argument("--db", default="traces.db")
        command.add_argument("--port", type=int, default=9000)
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
    args = parser.parse_args(argv)
    store = None
    try:
        store = TraceStore(args.db)
        if args.cmd == "list":
            print(render_list(store))
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
                create_app(store, "record", args.trace_id, args.name, args.upstream)
                if args.cmd == "record"
                else create_app(store, "replay", args.trace_id, replay_mode=args.mode)
            )
            print(f"trace_id={app.state.trace_id}")
            uvicorn.run(app, host="127.0.0.1", port=args.port)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.error(str(error))
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    main()
