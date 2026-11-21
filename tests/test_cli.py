import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_exportio import populated_trace

from agentreplay.cli import main
from agentreplay.inspection import render_list, render_trace
from agentreplay.runs import RunRecorder
from agentreplay.schema import Finding, Run
from agentreplay.store import TraceStore


def test_inspection_shows_exchanges_tools_runs_and_findings(store: TraceStore) -> None:
    trace = populated_trace(store)
    with RunRecorder(
        store, Run(kind="replay", trace_id=trace.id), clock=lambda: 100.0
    ) as run:
        store.add_finding(
            Finding(
                run_id=run.id,
                kind="stream_mismatch",
                severity="warning",
                exchange_seq=1,
                evidence={"requested_stream": False},
            )
        )
    listing = render_list(store)
    assert trace.id in listing and trace.name in listing
    assert (
        "2 exchanges" in listing and "2 tool events" in listing and "1 runs" in listing
    )
    shown = render_trace(store, trace.id)
    for expected in [
        "Exchange 0",
        "Exchange 1",
        "Tool 0: read",
        "Tool 1: fail",
        "ValueError: broken",
        "outcome=completed",
        "Finding: warning stream_mismatch",
        '"requested_stream": false',
        '"model": "mock"',
    ]:
        assert expected in shown
    with pytest.raises(ValueError, match="unknown trace"):
        render_trace(store, "absent")


def test_empty_list(store: TraceStore) -> None:
    assert render_list(store) == "No traces."


def test_cli_import_list_show_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_path, target_path = tmp_path / "source.db", tmp_path / "target.db"
    source = TraceStore(source_path)
    trace = populated_trace(source)
    source.close()
    exported, copied = tmp_path / "trace.jsonl", tmp_path / "copy.jsonl"
    main(
        [
            "export",
            "--db",
            str(source_path),
            "--trace-id",
            trace.id,
            "--out",
            str(exported),
        ]
    )
    assert "Exported" in capsys.readouterr().out
    main(["import", "--db", str(target_path), "--in", str(exported)])
    assert trace.id in capsys.readouterr().out
    main(["list", "--db", str(target_path)])
    assert "2 tool events" in capsys.readouterr().out
    main(["show", "--db", str(target_path), "--trace-id", trace.id])
    assert "Exchange 0" in capsys.readouterr().out
    main(
        [
            "export",
            "--db",
            str(target_path),
            "--trace-id",
            trace.id,
            "--out",
            str(copied),
        ]
    )
    assert "Exported" in capsys.readouterr().out
    assert copied.read_bytes() == exported.read_bytes()


@pytest.mark.parametrize(
    "case", ["missing_trace", "bad_file", "missing_file", "bad_schema", "usage"]
)
def test_cli_errors_have_exit_code_two_and_clear_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    database = tmp_path / "errors.db"
    command = ["show", "--db", str(database), "--trace-id", "absent"]
    expected = "unknown trace"
    if case == "bad_file":
        path = tmp_path / "broken.jsonl"
        path.write_text("broken\n")
        command = ["import", "--db", str(database), "--in", str(path)]
        expected = "line 1"
    elif case == "missing_file":
        command = ["import", "--db", str(database), "--in", str(tmp_path / "absent")]
        expected = "No such file"
    elif case == "bad_schema":
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE legacy (id TEXT)")
        command = ["list", "--db", str(database)]
        expected = "expected v"
    elif case == "usage":
        command = ["show"]
        expected = "required"
    with pytest.raises(SystemExit) as exited:
        main(command)
    assert exited.value.code == 2 and expected in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["record", "replay"])
def test_proxy_cli_wiring_closes_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    database = tmp_path / "serve.db"
    source = TraceStore(database)
    trace = populated_trace(source)
    source.close()
    active_store = TraceStore(database)
    monkeypatch.setattr("agentreplay.cli.TraceStore", lambda path: active_store)
    served: list[Any] = []

    def fake_serve(app: Any, host: str, port: int) -> None:
        assert host == "127.0.0.1" and port == 9123
        served.append(app)

    monkeypatch.setattr("agentreplay.cli.uvicorn.run", fake_serve)
    main([mode, "--db", str(database), "--trace-id", trace.id, "--port", "9123"])
    assert len(served) == 1 and f"trace_id={trace.id}" in capsys.readouterr().out
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        active_store.conn.execute("SELECT 1")
