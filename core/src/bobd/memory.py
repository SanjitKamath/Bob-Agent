"""Memory, in three kinds.

* Conversation memory: the last turns plus a rolling summary.
* Profile facts: explicit, stored as rows with date and source; list and delete them.
* Episodes: summaries of finished tasks, embedded so later work can learn from them.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np

from .db import Database, utcnow

log = logging.getLogger(__name__)

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]
Summarise = Callable[[str, str], Awaitable[str]]  # (previous summary, new transcript) -> summary

MAX_FACTS_IN_PROMPT = 40
MAX_TITLE = 80
# Conversations routines start for themselves are not chats; they stay out of the list.
ROUTINE_PREFIX = "routine:"


@dataclass(frozen=True)
class Turn:
    id: int
    role: str
    content: str
    specialist: str | None
    at: str
    source: str = "typed"


@dataclass(frozen=True)
class Fact:
    id: int
    fact: str
    source: str
    created_at: str


@dataclass(frozen=True)
class Episode:
    id: int
    specialist: str
    summary: str
    created_at: str
    score: float = 0.0


def provisional_title(question: str) -> str:
    """The first line of the first question, shortened at a word: the title until the
    model names the chat."""
    line = next((ln for ln in question.strip().splitlines() if ln.strip()), "").strip()
    line = " ".join(line.split())
    if len(line) <= 48:
        return line or "New chat"
    cut = line[:48].rsplit(" ", 1)[0]
    return (cut or line[:48]) + "…"


class Memory:
    def __init__(
        self,
        db: Database,
        *,
        embed: Embed | None = None,
        embedding_model: str = "",
        summarise: Summarise | None = None,
        history_turns: int = 8,
    ) -> None:
        self._db = db
        self._embed = embed
        self._embedding_model = embedding_model
        self._summarise = summarise
        self._history_turns = history_turns

    # -- conversation ---------------------------------------------------------

    def ensure_conversation(self, conversation_id: str | None) -> str:
        conversation_id = conversation_id or uuid.uuid4().hex
        self._db.execute(
            "INSERT OR IGNORE INTO conversations(id, created_at) VALUES (?, ?)",
            (conversation_id, utcnow()),
        )
        return conversation_id

    def add_turn(
        self,
        conversation_id: str,
        role: str,
        content: str,
        specialist: str | None = None,
        source: str = "typed",
    ) -> None:
        now = utcnow()
        self._db.execute(
            "INSERT INTO turns(conversation_id, at, role, content, specialist, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, now, role, content, specialist, source),
        )
        self._db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))

    # -- chat history ---------------------------------------------------------

    def start_chat(self, conversation_id: str | None, question: str) -> str:
        """A chat you started: give it a working title from its first question."""
        conversation_id = self.ensure_conversation(conversation_id)
        self._db.execute(
            "UPDATE conversations SET title=?, updated_at=? WHERE id=? AND title=''",
            (provisional_title(question), utcnow(), conversation_id),
        )
        return conversation_id

    def needs_title(self, conversation_id: str) -> bool:
        row = self._db.query_one("SELECT titled FROM conversations WHERE id=?", (conversation_id,))
        return row is not None and not row["titled"]

    def set_title(self, conversation_id: str, title: str, *, by_you: bool = False) -> bool:
        """Name a chat. The model's name never replaces one you gave it."""
        title = " ".join(title.split())[:MAX_TITLE].strip(" .\"'")
        if not title:
            return False
        sql = "UPDATE conversations SET title=?, titled=1 WHERE id=?"
        if not by_you:
            sql += " AND titled=0"
        return self._db.execute(sql, (title, conversation_id)).rowcount > 0

    def set_pinned(self, conversation_id: str, pinned: bool) -> bool:
        return (
            self._db.execute(
                "UPDATE conversations SET pinned=? WHERE id=?", (int(pinned), conversation_id)
            ).rowcount
            > 0
        )

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a chat and every message in it."""
        return (
            self._db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,)).rowcount
            > 0
        )

    def conversations(self, query: str = "", limit: int = 200) -> list[dict[str, object]]:
        """Your chats, pinned first, then the most recently active. ``query`` searches the
        titles and every message."""
        params: list[object] = [f"{ROUTINE_PREFIX}%"]
        where = "c.id NOT LIKE ? AND (c.title<>'' OR EXISTS (SELECT 1 FROM turns t WHERE t.conversation_id=c.id))"
        query = " ".join(query.split())[:200]
        if query:
            pattern = (
                "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            where += (
                " AND (c.title LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM turns t WHERE "
                "t.conversation_id=c.id AND t.content LIKE ? ESCAPE '\\'))"
            )
            params += [pattern, pattern]
        rows = self._db.query(
            "SELECT c.id, c.title, c.pinned, c.created_at, COALESCE(c.updated_at, c.created_at) "
            "AS updated_at, (SELECT COUNT(*) FROM turns t WHERE t.conversation_id=c.id) AS turns "
            f"FROM conversations c WHERE {where} "
            "ORDER BY c.pinned DESC, updated_at DESC LIMIT ?",
            (*params, max(1, min(limit, 500))),
        )
        found = []
        for row in rows:
            entry: dict[str, object] = {
                "id": row["id"],
                "title": row["title"] or "New chat",
                "pinned": bool(row["pinned"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "messages": row["turns"],
            }
            if query:
                entry["match"] = self._snippet(row["id"], query)
            found.append(entry)
        return found

    def _snippet(self, conversation_id: str, query: str) -> str:
        row = self._db.query_one(
            "SELECT content FROM turns WHERE conversation_id=? AND instr(lower(content), lower(?)) "
            "ORDER BY id DESC LIMIT 1",
            (conversation_id, query),
        )
        if row is None:
            return ""
        text = " ".join(row["content"].split())
        at = text.lower().find(query.lower())
        start = max(0, at - 40)
        return ("…" if start else "") + text[start : at + len(query) + 80]

    def conversation(self, conversation_id: str) -> dict[str, object] | None:
        """One chat with all of its messages, oldest first."""
        row = self._db.query_one(
            "SELECT id, title, pinned, created_at, COALESCE(updated_at, created_at) AS updated_at "
            "FROM conversations WHERE id=?",
            (conversation_id,),
        )
        if row is None:
            return None
        turns = self._db.query(
            "SELECT id, at, role, content, specialist FROM turns WHERE conversation_id=? "
            "ORDER BY id",
            (conversation_id,),
        )
        return {
            "id": row["id"],
            "title": row["title"] or "New chat",
            "pinned": bool(row["pinned"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "turns": [dict(turn) for turn in turns],
        }

    def summary(self, conversation_id: str) -> str:
        row = self._db.query_one("SELECT summary FROM conversations WHERE id=?", (conversation_id,))
        return row["summary"] if row else ""

    def recent_turns(self, conversation_id: str) -> list[Turn]:
        """Turns newer than the rolling summary, oldest first, capped to the history size."""
        row = self._db.query_one(
            "SELECT summarised_through FROM conversations WHERE id=?", (conversation_id,)
        )
        after = row["summarised_through"] if row else 0
        rows = self._db.query(
            "SELECT * FROM turns WHERE conversation_id=? AND id>? ORDER BY id DESC LIMIT ?",
            (conversation_id, after, self._history_turns * 2),
        )
        return [
            Turn(r["id"], r["role"], r["content"], r["specialist"], r["at"], r["source"])
            for r in reversed(rows)
        ]

    async def compact(self, conversation_id: str) -> bool:
        """Fold turns that fell out of the history window into the rolling summary."""
        if self._summarise is None:
            return False
        row = self._db.query_one(
            "SELECT summary, summarised_through FROM conversations WHERE id=?", (conversation_id,)
        )
        if row is None:
            return False
        rows = self._db.query(
            "SELECT id, role, content FROM turns WHERE conversation_id=? AND id>? ORDER BY id",
            (conversation_id, row["summarised_through"]),
        )
        keep = self._history_turns * 2
        if len(rows) <= keep:
            return False
        old = rows[: len(rows) - keep]
        transcript = "\n".join(f"{r['role']}: {r['content'][:2000]}" for r in old)
        try:
            summary = (await self._summarise(row["summary"], transcript)).strip()
        except Exception as exc:  # a failed summary must not lose the conversation
            log.warning("conversation summary failed: %s", exc)
            return False
        if not summary:
            return False
        self._db.execute(
            "UPDATE conversations SET summary=?, summarised_through=? WHERE id=?",
            (summary, old[-1]["id"], conversation_id),
        )
        return True

    # -- profile facts ----------------------------------------------------------

    def add_fact(self, fact: str, source: str = "user") -> Fact:
        fact = " ".join(fact.split())
        if not fact:
            raise ValueError("a fact cannot be empty")
        existing = self._db.query_one(
            "SELECT * FROM profile_facts WHERE lower(fact)=lower(?)", (fact,)
        )
        if existing is not None:
            return Fact(
                existing["id"], existing["fact"], existing["source"], existing["created_at"]
            )
        now = utcnow()
        cursor = self._db.execute(
            "INSERT INTO profile_facts(fact, source, created_at) VALUES (?, ?, ?)",
            (fact, source, now),
        )
        return Fact(int(cursor.lastrowid or 0), fact, source, now)

    def facts(self) -> list[Fact]:
        rows = self._db.query("SELECT * FROM profile_facts ORDER BY id")
        return [Fact(r["id"], r["fact"], r["source"], r["created_at"]) for r in rows]

    def delete_fact(self, fact_id: int) -> bool:
        return self._db.execute("DELETE FROM profile_facts WHERE id=?", (fact_id,)).rowcount > 0

    def facts_for_prompt(self) -> list[str]:
        return [fact.fact for fact in self.facts()[-MAX_FACTS_IN_PROMPT:]]

    # -- episodes ---------------------------------------------------------------

    async def add_episode(self, specialist: str, summary: str) -> None:
        summary = summary.strip()
        if not summary:
            return
        blob: bytes | None = None
        if self._embed is not None:
            try:
                vector = (await self._embed([summary]))[0]
                blob = np.asarray(vector, dtype=np.float32).tobytes()
            except Exception as exc:  # the episode is still worth keeping as text
                log.warning("episode embedding failed: %s", exc)
        self._db.execute(
            "INSERT INTO episodes(created_at, specialist, summary, embedding_model, embedding) "
            "VALUES (?, ?, ?, ?, ?)",
            (utcnow(), specialist, summary, self._embedding_model if blob else None, blob),
        )

    async def related_episodes(
        self, text: str, *, limit: int = 3, min_score: float = 0.5
    ) -> list[Episode]:
        if self._embed is None:
            return []
        rows = self._db.query(
            "SELECT * FROM episodes WHERE embedding IS NOT NULL AND embedding_model=?",
            (self._embedding_model,),
        )
        if not rows:
            return []
        try:
            query = np.asarray((await self._embed([text]))[0], dtype=np.float32)
        except Exception as exc:
            log.warning("episode lookup failed: %s", exc)
            return []
        norm = float(np.linalg.norm(query)) or 1.0
        scored: list[Episode] = []
        for row in rows:
            vector = np.frombuffer(row["embedding"], dtype=np.float32)
            if vector.shape != query.shape:
                continue
            score = float(vector @ query) / (norm * (float(np.linalg.norm(vector)) or 1.0))
            if score >= min_score:
                scored.append(
                    Episode(row["id"], row["specialist"], row["summary"], row["created_at"], score)
                )
        return sorted(scored, key=lambda e: e.score, reverse=True)[:limit]
