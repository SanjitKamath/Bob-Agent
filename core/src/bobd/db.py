"""SQLite storage shared by the core's subsystems.

One file, ``data/bob.sqlite``, in WAL mode. Each subsystem owns its tables and
goes through :class:`Database`, which serialises writes with a lock so the
service can use one connection from several threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    scope TEXT NOT NULL,          -- 'core' or a plugin id
    key   TEXT NOT NULL,
    value TEXT NOT NULL,          -- JSON
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,          -- who asked: 'user', 'routine:<id>', 'plugin:<id>'
    kind TEXT NOT NULL,           -- 'tool', 'git', 'plugin', 'conformance', 'settings'
    action TEXT NOT NULL,         -- what ran
    tier TEXT,
    arguments TEXT NOT NULL,      -- JSON
    outcome TEXT NOT NULL,        -- 'ok', 'denied', 'error', 'declined'
    detail TEXT NOT NULL DEFAULT ''
);
-- The audit table is append-only: no row may change or disappear.
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    summarised_through INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    titled INTEGER NOT NULL DEFAULT 0,  -- 1 once the model or you named it
    pinned INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    specialist TEXT
);
CREATE INDEX IF NOT EXISTS turns_by_conversation ON turns(conversation_id, id);

CREATE TABLE IF NOT EXISTS profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    specialist TEXT NOT NULL,
    summary TEXT NOT NULL,
    embedding_model TEXT,
    embedding BLOB
);

CREATE TABLE IF NOT EXISTS routine_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS routine_runs_by_routine ON routine_runs(routine_id, id);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,           -- e.g. 'brief', 'review', 'routine'
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS answer_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    question TEXT NOT NULL,
    normalized TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    collections TEXT NOT NULL DEFAULT '{}',
    embedding_model TEXT,
    embedding BLOB,
    reply TEXT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS answer_cache_key ON answer_cache(fingerprint, normalized);

CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id TEXT NOT NULL,
    at TEXT NOT NULL,
    metrics TEXT NOT NULL,        -- JSON
    passed INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if path != ":memory:" else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()

    # Columns added after a table was first created: (table, column, definition).
    _ADDED_COLUMNS = (
        ("conversations", "title", "TEXT NOT NULL DEFAULT ''"),
        ("conversations", "titled", "INTEGER NOT NULL DEFAULT 0"),
        ("conversations", "pinned", "INTEGER NOT NULL DEFAULT 0"),
        ("conversations", "updated_at", "TEXT"),
        # "typed" or "voice": what you said aloud is not what you wrote (see grants.py).
        ("turns", "source", "TEXT NOT NULL DEFAULT 'typed'"),
    )

    def _migrate(self) -> None:
        for table, column, definition in self._ADDED_COLUMNS:
            columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS conversations_by_update ON conversations(updated_at)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run several statements atomically."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # -- settings ---------------------------------------------------------

    def get_setting(self, scope: str, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM settings WHERE scope=? AND key=?", (scope, key))
        return default if row is None else json.loads(row["value"])

    def get_settings(self, scope: str) -> dict[str, Any]:
        rows = self.query("SELECT key, value FROM settings WHERE scope=?", (scope,))
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def set_setting(self, scope: str, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(scope, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (scope, key, json.dumps(value), utcnow()),
        )
