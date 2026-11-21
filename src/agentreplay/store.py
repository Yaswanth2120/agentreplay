"""SQLite trace storage; all database writes live here."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .canonical import canonical_json
from .schema import SCHEMA_VERSION, Exchange, Finding, Run, RunOutcome, ToolEvent, Trace

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, schema_version INTEGER NOT NULL,
    created_at REAL NOT NULL, task_id TEXT, agent_name TEXT,
    agent_version TEXT, model TEXT, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchanges (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, request TEXT NOT NULL,
    strict_hash TEXT NOT NULL, lenient_hash TEXT NOT NULL,
    stream INTEGER NOT NULL DEFAULT 0, status_code INTEGER NOT NULL,
    response TEXT, chunks TEXT, error TEXT,
    latency_ms REAL NOT NULL, created_at REAL NOT NULL,
    UNIQUE (trace_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_ex_strict ON exchanges (trace_id, strict_hash);
CREATE INDEX IF NOT EXISTS idx_ex_lenient ON exchanges (trace_id, lenient_hash);
CREATE TABLE IF NOT EXISTS tool_events (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, name TEXT NOT NULL, args TEXT NOT NULL,
    args_hash TEXT NOT NULL, result TEXT, error TEXT,
    latency_ms REAL NOT NULL, created_at REAL NOT NULL,
    UNIQUE (trace_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_tool_match ON tool_events (trace_id, name, args_hash);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('record','live','replay')),
    trace_id TEXT NOT NULL REFERENCES traces(id),
    replay_mode TEXT CHECK (replay_mode IN ('strict','lenient')),
    task_id TEXT, trial INTEGER, agent_name TEXT, agent_version TEXT, model TEXT,
    config TEXT NOT NULL DEFAULT '{}', fault_plan_id TEXT,
    started_at REAL NOT NULL, finished_at REAL,
    outcome TEXT CHECK (outcome IN ('completed','agent_error','aborted')),
    task_success INTEGER, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs (task_id, agent_version, model);
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
    exchange_seq INTEGER, tool_seq INTEGER,
    evidence TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings (run_id, kind);
"""


class SchemaVersionError(ValueError):
    """The database requires a different version of AgentReplay."""


class TraceStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._reserved: dict[str, int] = {}
        self._tool_reserved: dict[str, int] = {}
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            tables = {
                row[0]
                for row in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if tables:
                version = None
                if "schema_meta" in tables:
                    row = self.conn.execute(
                        "SELECT value FROM schema_meta WHERE key='schema_version'"
                    ).fetchone()
                    version = row[0] if row else None
                if version != str(SCHEMA_VERSION):
                    raise SchemaVersionError(
                        f"database schema {version or 'legacy/unversioned'}; "
                        f"expected v{SCHEMA_VERSION}. Use a fresh database; no migration is provided."
                    )
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(DDL)
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
        except (sqlite3.Error, SchemaVersionError):
            self.conn.close()
            raise

    def close(self) -> None:
        self.conn.close()

    def create_trace(self, trace: Trace) -> Trace:
        with self._lock, self.conn:
            self._insert_trace(trace)
        return trace

    def _insert_trace(self, trace: Trace) -> None:
        self.conn.execute(
            "INSERT INTO traces VALUES (?,?,?,?,?,?,?,?,?)",
            (
                trace.id,
                trace.name,
                trace.schema_version,
                trace.created_at,
                trace.task_id,
                trace.agent_name,
                trace.agent_version,
                trace.model,
                canonical_json(trace.metadata),
            ),
        )

    def get_trace(self, trace_id: str) -> Trace | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM traces WHERE id=?", (trace_id,)
            ).fetchone()
        if row is None:
            return None
        if row["schema_version"] != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"trace {trace_id} has schema v{row['schema_version']}, expected v{SCHEMA_VERSION}"
            )
        return Trace(**{**dict(row), "metadata": json.loads(row["metadata"])})

    def list_traces(self) -> list[Trace]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM traces ORDER BY created_at"
            ).fetchall()
        return [
            trace for row in rows if (trace := self.get_trace(row["id"])) is not None
        ]

    def next_seq(self, trace_id: str) -> int:
        # Reserve before upstream I/O: completion order must not change request order.
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM exchanges WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            seq = max(int(row[0]), self._reserved.get(trace_id, 0))
            self._reserved[trace_id] = seq + 1
            return seq

    def add_exchange(self, ex: Exchange) -> None:
        with self._lock, self.conn:
            self._insert_exchange(ex)

    def _insert_exchange(self, ex: Exchange) -> None:
        self.conn.execute(
            "INSERT INTO exchanges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ex.id,
                ex.trace_id,
                ex.seq,
                canonical_json(ex.request),
                ex.strict_hash,
                ex.lenient_hash,
                int(ex.stream),
                ex.status_code,
                canonical_json(ex.response) if ex.response is not None else None,
                canonical_json(ex.chunks) if ex.chunks is not None else None,
                ex.error,
                ex.latency_ms,
                ex.created_at,
            ),
        )

    def exchanges(self, trace_id: str) -> list[Exchange]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM exchanges WHERE trace_id=? ORDER BY seq",
                (trace_id,),
            ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> Exchange:
        values = dict(row)
        for key in ("request", "response", "chunks"):
            values[key] = json.loads(values[key]) if values[key] is not None else None
        return Exchange(**values)

    def create_run(self, run: Run) -> Run:
        values = run.model_dump()
        values["config"] = canonical_json(run.config)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        with self._lock, self.conn:
            self.conn.execute(
                f"INSERT INTO runs ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        return run

    def get_run(self, run_id: str) -> Run | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        return self._run_row(row) if row is not None else None

    @staticmethod
    def _run_row(row: sqlite3.Row) -> Run:
        return Run(**{**dict(row), "config": json.loads(row["config"])})

    def runs(self, trace_id: str) -> list[Run]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE trace_id=? ORDER BY started_at, id",
                (trace_id,),
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def finish_run(
        self,
        run_id: str,
        outcome: RunOutcome,
        *,
        finished_at: float,
        task_success: bool | None = None,
        detail: str | None = None,
        findings: list[Finding] | None = None,
    ) -> Run:
        with self._lock, self.conn:
            cursor = self.conn.execute(
                "UPDATE runs SET finished_at=?, outcome=?, task_success=?, detail=? "
                "WHERE id=? AND finished_at IS NULL",
                (finished_at, outcome, task_success, detail, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"run {run_id!r} is unknown or already finished")
            for finding in findings or []:
                if finding.run_id != run_id:
                    raise ValueError(
                        "finish findings must belong to the run being finished"
                    )
                self._insert_finding(finding)
            return self._run_row(
                self.conn.execute(
                    "SELECT * FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
            )

    def add_finding(self, finding: Finding) -> None:
        with self._lock, self.conn:
            self._insert_finding(finding)

    def _insert_finding(self, finding: Finding) -> None:
        self.conn.execute(
            "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?)",
            (
                finding.id,
                finding.run_id,
                finding.kind,
                finding.severity,
                finding.exchange_seq,
                finding.tool_seq,
                canonical_json(finding.evidence),
                finding.created_at,
            ),
        )

    def findings(self, run_id: str) -> list[Finding]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM findings WHERE run_id=? ORDER BY created_at, rowid",
                (run_id,),
            ).fetchall()
        return [
            Finding(**{**dict(row), "evidence": json.loads(row["evidence"])})
            for row in rows
        ]

    def next_tool_seq(self, trace_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM tool_events WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            seq = max(int(row[0]), self._tool_reserved.get(trace_id, 0))
            self._tool_reserved[trace_id] = seq + 1
            return seq

    def add_tool_event(self, event: ToolEvent) -> None:
        with self._lock, self.conn:
            self._insert_tool_event(event)

    def _insert_tool_event(self, event: ToolEvent) -> None:
        self.conn.execute(
            "INSERT INTO tool_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                event.id,
                event.trace_id,
                event.seq,
                event.name,
                canonical_json(event.args),
                event.args_hash,
                canonical_json(event.result) if event.error is None else None,
                event.error,
                event.latency_ms,
                event.created_at,
            ),
        )

    def tool_events(self, trace_id: str) -> list[ToolEvent]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM tool_events WHERE trace_id=? ORDER BY seq",
                (trace_id,),
            ).fetchall()
        return [
            ToolEvent(
                **{
                    **dict(row),
                    "args": json.loads(row["args"]),
                    "result": json.loads(row["result"])
                    if row["result"] is not None
                    else None,
                }
            )
            for row in rows
        ]

    def import_trace(
        self,
        trace: Trace,
        exchanges: list[Exchange],
        events: list[ToolEvent],
    ) -> None:
        with self._lock, self.conn:
            self._insert_trace(trace)
            for exchange in exchanges:
                self._insert_exchange(exchange)
            for event in events:
                self._insert_tool_event(event)
