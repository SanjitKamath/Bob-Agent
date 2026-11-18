"""The task list, in SQLite."""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from bob_sdk.runtime import ToolError

Priority = Literal["high", "normal", "low"]
Status = Literal["open", "done", "all"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('high', 'normal', 'low')),
    due TEXT,                      -- ISO date
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
"""
_PRIORITY_ORDER = "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)]|\[ ?\])\s+(.*\S)\s*$")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_due(value: str | None) -> str | None:
    """Accept an ISO date, or 'today' / 'tomorrow'. Empty clears the date."""
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text == "today":
        return date.today().isoformat()
    if text == "tomorrow":
        return (date.today() + timedelta(days=1)).isoformat()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        raise ToolError(f"due date {value!r} must be YYYY-MM-DD, 'today' or 'tomorrow'") from None


def parse_task_lines(text: str) -> list[str]:
    """Pull task titles out of bullet or numbered lines."""
    titles: list[str] = []
    for line in text.splitlines():
        match = _BULLET.match(line)
        if match:
            title = match.group(1).strip()
            if title and title.lower() not in {"none", "no tasks"} and title not in titles:
                titles.append(title[:300])
    return titles


class TaskStore:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _row(self, task_id: int) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ToolError(f"there is no task {task_id}")
        return dict(row)

    def create(
        self,
        title: str,
        *,
        due: str | None = None,
        priority: Priority = "normal",
        notes: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        title = " ".join(title.split())
        if not title:
            raise ToolError("a task needs a title")
        if priority not in ("high", "normal", "low"):
            raise ToolError("priority must be high, normal or low")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO tasks(title, notes, priority, due, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (title[:300], notes, priority, parse_due(due), source, _now()),
            )
            return self._row(int(cursor.lastrowid or 0))

    def update(
        self,
        task_id: int,
        *,
        title: str | None = None,
        due: str | None = None,
        priority: Priority | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Change the given fields. ``due=""`` clears the due date."""
        with self._lock:
            current = self._row(task_id)
            if title is not None:
                title = " ".join(title.split())
                if not title:
                    raise ToolError("a task needs a title")
                current["title"] = title[:300]
            if due is not None:
                current["due"] = parse_due(due)
            if priority is not None:
                if priority not in ("high", "normal", "low"):
                    raise ToolError("priority must be high, normal or low")
                current["priority"] = priority
            if notes is not None:
                current["notes"] = notes
            self._conn.execute(
                "UPDATE tasks SET title=?, due=?, priority=?, notes=? WHERE id=?",
                (current["title"], current["due"], current["priority"], current["notes"], task_id),
            )
            return self._row(task_id)

    def complete(self, task_id: int, done: bool = True) -> dict[str, Any]:
        with self._lock:
            self._row(task_id)
            self._conn.execute(
                "UPDATE tasks SET completed_at=? WHERE id=?", (_now() if done else None, task_id)
            )
            return self._row(task_id)

    def list(self, status: Status = "open", limit: int = 50) -> list[dict[str, Any]]:
        where = {"open": "WHERE completed_at IS NULL", "done": "WHERE completed_at IS NOT NULL"}
        order = (
            "ORDER BY completed_at DESC"
            if status == "done"
            else f"ORDER BY completed_at IS NOT NULL, {_PRIORITY_ORDER}, due IS NULL, due, id"
        )
        rows = self._conn.execute(
            f"SELECT * FROM tasks {where.get(status, '')} {order} LIMIT ?",  # noqa: S608 - fixed fragments
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        today = date.today().isoformat()
        open_tasks = self.list("open", 500)
        due_today = [t for t in open_tasks if t["due"] == today]
        overdue = [t for t in open_tasks if t["due"] and t["due"] < today]
        status = f"Next: {open_tasks[0]['title']}" if open_tasks else "Nothing open"
        return {
            "counts": {
                "open": len(open_tasks),
                "due_today": len(due_today),
                "overdue": len(overdue),
            },
            "status": status,
            "items": [self._line(task, today) for task in open_tasks],
        }

    @staticmethod
    def _line(task: dict[str, Any], today: str) -> dict[str, str]:
        """One open task as a card line: its title, when it is due, and whether it is late."""
        due = task["due"] or ""
        flag = "overdue" if due and due < today else "today" if due == today else ""
        detail = "due today" if flag == "today" else f"due {due}" if due else ""
        if task["priority"] == "high":
            detail = f"high priority, {detail}" if detail else "high priority"
        return {"title": task["title"], "detail": detail, "flag": flag}
