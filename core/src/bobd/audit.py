"""The append-only audit log: who asked, what ran, the arguments, the result."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .db import Database, utcnow

_MAX_DETAIL = 4000
_REDACTED_KEYS = ("password", "token", "secret", "authorization")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if any(word in key.lower() for word in _REDACTED_KEYS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


@dataclass(frozen=True)
class AuditEntry:
    id: int
    at: str
    actor: str
    kind: str
    action: str
    tier: str | None
    arguments: dict[str, Any]
    outcome: str
    detail: str


class AuditLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        actor: str,
        kind: str,
        action: str,
        outcome: str,
        tier: str | None = None,
        arguments: dict[str, Any] | None = None,
        detail: str = "",
    ) -> int:
        cursor = self._db.execute(
            "INSERT INTO audit_log(at, actor, kind, action, tier, arguments, outcome, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                utcnow(),
                actor,
                kind,
                action,
                tier,
                json.dumps(_redact(arguments or {}), default=str),
                outcome,
                detail[:_MAX_DETAIL],
            ),
        )
        return int(cursor.lastrowid or 0)

    def entries(self, *, limit: int = 100, kind: str | None = None) -> list[AuditEntry]:
        sql = "SELECT * FROM audit_log"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        return [
            AuditEntry(
                id=row["id"],
                at=row["at"],
                actor=row["actor"],
                kind=row["kind"],
                action=row["action"],
                tier=row["tier"],
                arguments=json.loads(row["arguments"]),
                outcome=row["outcome"],
                detail=row["detail"],
            )
            for row in self._db.query(sql, params)
        ]
