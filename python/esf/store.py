"""SQLite storage for projects, papers, review decisions and token usage."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    paragraph TEXT NOT NULL,
    spec TEXT,
    status TEXT NOT NULL,
    progress TEXT,
    error TEXT,
    search_plan TEXT,
    search_log TEXT,
    overlap TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    ext_id TEXT NOT NULL,
    pmid TEXT,
    pmcid TEXT,
    doi TEXT,
    nct_ids TEXT,
    title TEXT,
    authors TEXT,
    year TEXT,
    journal TEXT,
    pubtypes TEXT,
    abstract TEXT,
    found_by TEXT,
    found_in TEXT,
    cited_by TEXT,
    status TEXT NOT NULL,
    screen TEXT,
    fulltext_kind TEXT,
    fulltext_url TEXT,
    extraction TEXT,
    extracted_by TEXT,
    verification TEXT,
    escalation TEXT,
    error TEXT,
    UNIQUE (project_id, source, ext_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    result_index INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (paper_id, result_index)
);
CREATE TABLE IF NOT EXISTS usage (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    step TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_input_tokens INTEGER NOT NULL,
    cache_creation_input_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS papers_project ON papers(project_id);
CREATE INDEX IF NOT EXISTS usage_project ON usage(project_id);
"""

JSON_COLUMNS = {
    "spec",
    "progress",
    "search_plan",
    "search_log",
    "overlap",
    "summary",
    "nct_ids",
    "pubtypes",
    "found_by",
    "found_in",
    "cited_by",
    "screen",
    "extraction",
    "verification",
    "escalation",
}

# Statuses of a project while a job is working on it.
RUNNING = {"reading", "searching", "screening", "extracting"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for key in JSON_COLUMNS & out.keys():
        if out[key] is not None:
            out[key] = json.loads(out[key])
    return out


def _encode(key: str, value: Any) -> Any:
    return json.dumps(value) if key in JSON_COLUMNS and value is not None else value


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            if str(path) != ":memory:":
                self._db.execute("PRAGMA journal_mode = WAL")
            self._db.executescript(SCHEMA)
            # Columns added since the first version; CREATE TABLE IF NOT EXISTS leaves old tables as they were.
            for table, column in (
                ("projects", "search_log"), ("projects", "overlap"), ("projects", "summary"),
                ("papers", "found_in"), ("papers", "authors"), ("papers", "cited_by"),
            ):
                if column not in {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._db.execute(sql, args)
            self._db.commit()
            return cursor

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, args).fetchall()]  # type: ignore[misc]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        with self._lock:
            return _row(self._db.execute(sql, args).fetchone())

    # -- projects -------------------------------------------------------------

    def create_project(self, paragraph: str) -> str:
        project_id = uuid.uuid4().hex[:12]
        self._exec(
            "INSERT INTO projects (id, created_at, paragraph, status) VALUES (?, ?, ?, ?)",
            (project_id, now(), paragraph, "new"),
        )
        return project_id

    def project(self, project_id: str) -> dict | None:
        return self._one("SELECT * FROM projects WHERE id = ?", (project_id,))

    def projects(self) -> list[dict]:
        return self._all("SELECT id, created_at, paragraph, status FROM projects ORDER BY created_at DESC")

    def update_project(self, project_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = tuple(_encode(key, value) for key, value in fields.items())
        self._exec(f"UPDATE projects SET {columns} WHERE id = ?", (*values, project_id))

    def interrupt_running(self) -> None:
        """After a restart, no job is running any more."""
        placeholders = ", ".join("?" for _ in RUNNING)
        self._exec(
            f"UPDATE projects SET status = 'interrupted', error = 'The server restarted while this was running.' "
            f"WHERE status IN ({placeholders})",
            tuple(RUNNING),
        )
        self._exec(
            "UPDATE papers SET status = 'error', error = 'The server restarted while this was being extracted.' "
            "WHERE status IN ('fetching', 'extracting')"
        )
        for row in self._all("SELECT id, summary FROM projects WHERE summary IS NOT NULL"):
            if row["summary"].get("status") == "running":
                self.update_project(row["id"], summary={
                    "status": "error", "error": "The server restarted while the summary was being written.",
                })

    # -- papers ---------------------------------------------------------------

    def add_paper(self, project_id: str, **fields: Any) -> int | None:
        """Insert a paper; returns None when the project already has it."""
        columns = ["project_id", *fields]
        values = [project_id, *(_encode(k, v) for k, v in fields.items())]
        cursor = self._exec(
            f"INSERT OR IGNORE INTO papers ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            tuple(values),
        )
        return cursor.lastrowid if cursor.rowcount else None

    def paper(self, paper_id: int) -> dict | None:
        return self._one("SELECT * FROM papers WHERE id = ?", (paper_id,))

    def papers(self, project_id: str, status: str | None = None) -> list[dict]:
        if status:
            return self._all("SELECT * FROM papers WHERE project_id = ? AND status = ? ORDER BY id", (project_id, status))
        return self._all("SELECT * FROM papers WHERE project_id = ? ORDER BY id", (project_id,))

    def update_paper(self, paper_id: int, **fields: Any) -> None:
        columns = ", ".join(f"{key} = ?" for key in fields)
        values = tuple(_encode(key, value) for key, value in fields.items())
        self._exec(f"UPDATE papers SET {columns} WHERE id = ?", (*values, paper_id))

    def clear_papers(self, project_id: str) -> None:
        self._exec("DELETE FROM papers WHERE project_id = ?", (project_id,))

    def status_counts(self, project_id: str) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM papers WHERE project_id = ? GROUP BY status", (project_id,))
        return {r["status"]: r["n"] for r in rows}

    # -- decisions ------------------------------------------------------------

    def set_decision(self, paper_id: int, result_index: int, decision: str | None, reason: str = "") -> None:
        if decision is None:
            self._exec("DELETE FROM decisions WHERE paper_id = ? AND result_index = ?", (paper_id, result_index))
            return
        self._exec(
            "INSERT INTO decisions (paper_id, result_index, decision, reason, decided_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (paper_id, result_index) DO UPDATE SET decision = excluded.decision, "
            "reason = excluded.reason, decided_at = excluded.decided_at",
            (paper_id, result_index, decision, reason, now()),
        )

    def clear_decisions(self, paper_id: int) -> None:
        self._exec("DELETE FROM decisions WHERE paper_id = ?", (paper_id,))

    def decisions(self, project_id: str) -> dict[int, dict[int, dict]]:
        rows = self._all(
            "SELECT d.* FROM decisions d JOIN papers p ON p.id = d.paper_id WHERE p.project_id = ?", (project_id,)
        )
        out: dict[int, dict[int, dict]] = {}
        for r in rows:
            out.setdefault(r["paper_id"], {})[r["result_index"]] = {
                "decision": r["decision"],
                "reason": r["reason"],
                "decided_at": r["decided_at"],
            }
        return out

    # -- usage ----------------------------------------------------------------

    def add_usage(self, project_id: str, step: str, model: str, usage: dict[str, int]) -> None:
        self._exec(
            "INSERT INTO usage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                step,
                model,
                usage["input_tokens"],
                usage["output_tokens"],
                usage["cache_read_input_tokens"],
                usage["cache_creation_input_tokens"],
                now(),
            ),
        )

    def usage(self, project_id: str) -> list[dict]:
        return self._all(
            "SELECT step, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, SUM(cache_read_input_tokens) AS cache_read_input_tokens, "
            "SUM(cache_creation_input_tokens) AS cache_creation_input_tokens "
            "FROM usage WHERE project_id = ? GROUP BY step, model ORDER BY MIN(created_at)",
            (project_id,),
        )
