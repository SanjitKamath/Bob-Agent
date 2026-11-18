"""A record of every change Bob made to your files, so each can be undone."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    at REAL NOT NULL,
    op TEXT NOT NULL,           -- write | edit | mkdir | move | delete
    path TEXT NOT NULL,
    dest TEXT,                  -- move only
    existed INTEGER NOT NULL,   -- the path existed before the change
    backup TEXT,                -- where the original was kept
    after_sha TEXT,             -- the file's hash right after the change
    undone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS changes_session ON changes(session, id);
"""


class Journal:
    def __init__(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.backups = folder / "backups"
        self.backups.mkdir(exist_ok=True)
        self._db = sqlite3.connect(folder / "journal.sqlite", isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def backup_slot(self) -> Path:
        """A fresh, empty folder for one change's original."""
        slot = self.backups / f"{time.time_ns()}"
        slot.mkdir()
        return slot

    def record(
        self,
        session: str,
        op: str,
        path: Path,
        *,
        dest: Path | None = None,
        existed: bool,
        backup: Path | None = None,
        after_sha: str | None = None,
    ) -> int:
        cursor = self._db.execute(
            "INSERT INTO changes(session, at, op, path, dest, existed, backup, after_sha)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                session, time.time(), op, str(path), str(dest) if dest else None,
                int(existed), str(backup) if backup else None, after_sha,
            ),
        )  # fmt: skip
        return int(cursor.lastrowid or 0)

    def pending(self, session: str, steps: int) -> list[sqlite3.Row]:
        """The newest changes in a chat not yet undone, newest first."""
        return self._db.execute(
            "SELECT * FROM changes WHERE session=? AND undone=0 ORDER BY id DESC LIMIT ?",
            (session, steps),
        ).fetchall()

    def mark_undone(self, change_id: int) -> None:
        self._db.execute("UPDATE changes SET undone=1 WHERE id=?", (change_id,))

    def entries(self, session: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM changes WHERE session=? ORDER BY id DESC LIMIT ?", (session, limit)
        ).fetchall()
        return [
            {
                "id": r["id"], "at": r["at"], "op": r["op"], "path": r["path"],
                "to": r["dest"], "undone": bool(r["undone"]),
            }
            for r in rows
        ]  # fmt: skip
