"""Append-only audit store for decisions (SQLite).

Each row holds the full decision record and the exact engine input, so a decision can be
replayed later against the recorded rules version. Monitoring data goes in a separate table
that the decision path never reads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    rules_sha256 TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    record TEXT NOT NULL,
    input TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TABLE IF NOT EXISTS monitoring (
    application_id TEXT PRIMARY KEY,
    protected_group TEXT NOT NULL
);
"""


class DecisionStore:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def save(self, record: dict, engine_input: dict) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    record["decision_id"],
                    record["application_id"],
                    record["outcome"],
                    record["rules_version"],
                    record["rules_sha256"],
                    record["decided_at"],
                    record["input_sha256"],
                    json.dumps(record, default=str),
                    json.dumps(engine_input, default=str),
                ),
            )

    def save_monitoring(self, application_id: str, group: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO monitoring VALUES (?,?)", (application_id, group))

    def get(self, decision_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT record, input FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        if not row:
            return None
        record = json.loads(row[0])
        record["input"] = json.loads(row[1])
        return record

    def count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
