"""The answer cache: reuse a reply when the same, or a very similar, question comes back.

Only answers that cannot go stale are kept. The orchestrator decides that (no tools, or
only knowledge searches); this module also checks, before reusing an answer, that every
collection it searched is still at the version it was indexed at when the answer was made.
Mail, tasks, files and anything that acts are never cached, so "what are my latest emails"
is always answered fresh.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from .config import CacheConfig
from .db import Database, utcnow

log = logging.getLogger(__name__)

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]
# collection ids -> {collection: the indexed version of each of its sources}
Versions = Callable[[list[str]], Awaitable[dict[str, Any]]]

_WORDS = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    """The cache key for a question: lower case, words only."""
    return " ".join(_WORDS.findall(text.lower()))


# Words that point back at the conversation: a question with one of them is a follow-up.
_REFERS_BACK = {
    "it", "its", "that", "this", "these", "those", "they", "them", "their", "theirs", "he",
    "she", "him", "his", "her", "hers", "above", "previous", "earlier", "again", "more",
    "else", "same", "instead", "former", "latter", "one", "ones", "there", "then", "too",
}  # fmt: skip
_FOLLOW_UP_START = {
    "and",
    "but",
    "so",
    "also",
    "or",
    "what about",
    "how about",
    "why",
    "ok",
    "okay",
}


def is_self_contained(text: str) -> bool:
    """True when a question can be understood without the conversation before it.

    Cautious on purpose: a question that might lean on what was said earlier is not
    reused, because a wrong cached answer is worse than a slow right one.
    """
    words = _WORDS.findall(text.lower().replace("'", " "))
    if len(words) < 3:
        return False
    if any(word in _REFERS_BACK for word in words):
        return False
    start = " ".join(words[:2])
    return words[0] not in _FOLLOW_UP_START and start not in _FOLLOW_UP_START


def fingerprint(parts: dict[str, Any]) -> str:
    """A short hash of everything that would change an answer: plugins, models, facts."""
    blob = json.dumps(parts, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:20]


def _cutoff(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


@dataclass(frozen=True)
class CachedAnswer:
    id: int
    question: str
    created_at: str
    reply: dict[str, Any]
    similarity: float


class AnswerCache:
    def __init__(
        self,
        db: Database,
        config: CacheConfig,
        *,
        versions: Versions,
        embed: Embed | None = None,
        embedding_model: str = "",
    ) -> None:
        self._db = db
        self._config = config
        self._versions = versions
        self._embed = embed
        self._embedding_model = embedding_model

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def semantic(self) -> bool:
        return self._config.similarity < 1.0 and self._embed is not None

    async def _vector(self, text: str) -> np.ndarray | None:
        if not self.semantic:
            return None
        try:
            return np.asarray((await self._embed([text]))[0], dtype=np.float32)  # type: ignore[misc]
        except Exception as exc:  # the cache is an optimisation; never fail a request for it
            log.warning("answer cache: embedding failed: %s", exc)
            return None

    async def lookup(self, text: str, key: str) -> tuple[CachedAnswer | None, np.ndarray | None]:
        """A reusable answer for this question, and the question's embedding (if computed)."""
        normalized = normalize(text)
        if not self.enabled or not normalized:
            return None, None
        cutoff = _cutoff(self._config.max_age_hours)
        row = self._db.query_one(
            "SELECT * FROM answer_cache WHERE fingerprint=? AND normalized=? AND created_at>=? "
            "ORDER BY id DESC LIMIT 1",
            (key, normalized, cutoff),
        )
        score = 1.0
        vector: np.ndarray | None = None
        if row is None and self.semantic:
            rows = self._db.query(
                "SELECT * FROM answer_cache WHERE fingerprint=? AND created_at>=? "
                "AND embedding IS NOT NULL AND embedding_model=?",
                (key, cutoff, self._embedding_model),
            )
            if rows:
                vector = await self._vector(text)
            if vector is not None:
                norm = float(np.linalg.norm(vector)) or 1.0
                best: tuple[float, Any] | None = None
                for candidate in rows:
                    stored = np.frombuffer(candidate["embedding"], dtype=np.float32)
                    if stored.shape != vector.shape:
                        continue
                    similarity = float(stored @ vector) / (
                        norm * (float(np.linalg.norm(stored)) or 1.0)
                    )
                    if best is None or similarity > best[0]:
                        best = (similarity, candidate)
                if best is not None and best[0] >= self._config.similarity:
                    score, row = best
        if row is None:
            return None, vector
        collections = json.loads(row["collections"] or "{}")
        if collections:
            try:
                current = await self._versions(sorted(collections))
            except Exception as exc:
                log.info("answer cache: cannot check collection versions (%s); not reusing", exc)
                return None, vector
            if current != collections:
                # The knowledge it was built on changed: this answer is stale for good.
                self._db.execute("DELETE FROM answer_cache WHERE id=?", (row["id"],))
                return None, vector
        self._db.execute("UPDATE answer_cache SET hits=hits+1 WHERE id=?", (row["id"],))
        return (
            CachedAnswer(
                id=row["id"],
                question=row["question"],
                created_at=row["created_at"],
                reply=json.loads(row["reply"]),
                similarity=round(score, 3),
            ),
            vector,
        )

    async def store(
        self,
        text: str,
        key: str,
        reply: dict[str, Any],
        collections: list[str],
        vector: np.ndarray | None = None,
    ) -> bool:
        normalized = normalize(text)
        if not self.enabled or not normalized:
            return False
        snapshot: dict[str, Any] = {}
        if collections:
            try:
                snapshot = await self._versions(sorted(set(collections)))
            except Exception as exc:
                log.info("answer cache: cannot read collection versions (%s); not storing", exc)
                return False
        if vector is None:
            vector = await self._vector(text)
        self._db.execute(
            "INSERT INTO answer_cache(created_at, question, normalized, fingerprint, collections, "
            "embedding_model, embedding, reply) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                utcnow(),
                text[:2000],
                normalized,
                key,
                json.dumps(snapshot, sort_keys=True),
                self._embedding_model if vector is not None else None,
                vector.astype(np.float32).tobytes() if vector is not None else None,
                json.dumps(reply, default=str),
            ),
        )
        self._prune()
        return True

    def _prune(self) -> None:
        self._db.execute(
            "DELETE FROM answer_cache WHERE created_at<?", (_cutoff(self._config.max_age_hours),)
        )
        self._db.execute(
            "DELETE FROM answer_cache WHERE id NOT IN "
            "(SELECT id FROM answer_cache ORDER BY id DESC LIMIT ?)",
            (self._config.max_entries,),
        )

    def clear(self) -> int:
        return self._db.execute("DELETE FROM answer_cache").rowcount

    def stats(self) -> dict[str, Any]:
        row = self._db.query_one(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(hits), 0) AS hits FROM answer_cache"
        )
        return {
            "enabled": self.enabled,
            "entries": row["entries"] if row else 0,
            "hits": row["hits"] if row else 0,
            "similarity": self._config.similarity,
            "max_age_hours": self._config.max_age_hours,
        }
