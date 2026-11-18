"""Folders you let Bob work in, one chat at a time.

A grant exists only when you wrote the folder's path in the chat and approved the card that
asked. It covers that folder and everything inside it (or just the one file, when you named a
file), for that chat only, and is gone when bobd restarts, the chat is deleted or you revoke it.
A model cannot ask for a folder you did not name: the broker asks the store which of your own
words name it.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bob_sdk.output import is_within
from bob_sdk.writeguard import Refused, check_grant

# Grants end after this long even if the chat goes on.
GRANT_SECONDS = 12 * 3600

# A path written in a message: quoted (spaces allowed) or bare (up to the next space).
_QUOTED = re.compile(r"""["'`]((?:~|/)[^"'`\n]*)["'`]""")
_BARE = re.compile(r"""(?<![\w.:/~-])((?:~|/)[^\s"'`<>|*?(){}\[\],;]*)""")
_TRAILING = ".:!?"


def named_paths(said: str) -> list[Path]:
    """The folders and files a message names, as absolute paths, in the order written."""
    found: list[str] = []
    for match in _QUOTED.finditer(said):
        found.append(match.group(1))
    quoted_spans = [m.span() for m in _QUOTED.finditer(said)]
    for match in _BARE.finditer(said):
        if any(start <= match.start() < end for start, end in quoted_spans):
            continue
        found.append(match.group(1).rstrip(_TRAILING))
    paths: list[Path] = []
    for raw in found:
        raw = raw.strip()
        if len(raw) < 2 or raw in ("~", "/"):
            continue  # "/" alone is a slash in prose, "~" alone is your whole home
        path = Path(os.path.expanduser(raw)).resolve()
        if path not in paths:
            paths.append(path)
    return paths


@dataclass(frozen=True)
class Grant:
    id: str
    session: str
    root: Path
    file_only: bool  # you named one file, not a folder
    created: float = field(default_factory=time.time)

    def covers(self, path: Path) -> bool:
        if time.time() - self.created > GRANT_SECONDS:
            return False
        return path == self.root if self.file_only else is_within(path, self.root)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session": self.session,
            "path": str(self.root),
            "kind": "file" if self.file_only else "folder",
            "created": self.created,
        }


class GrantStore:
    def __init__(self) -> None:
        self._grants: list[Grant] = []

    def covering(self, session: str, path: Path) -> Grant | None:
        for grant in self._grants:
            if grant.session == session and grant.covers(path):
                return grant
        return None

    def add(self, session: str, root: Path, *, file_only: bool) -> Grant:
        grant = Grant(secrets.token_urlsafe(6), session, root, file_only)
        self._grants.append(grant)
        return grant

    def list(self, session: str | None = None) -> list[Grant]:
        now = time.time()
        live = [g for g in self._grants if now - g.created <= GRANT_SECONDS]
        self._grants = live
        return [g for g in live if session is None or g.session == session]

    def revoke(self, grant_id: str) -> bool:
        before = len(self._grants)
        self._grants = [g for g in self._grants if g.id != grant_id]
        return len(self._grants) < before

    def revoke_session(self, session: str) -> int:
        before = len(self._grants)
        self._grants = [g for g in self._grants if g.session != session]
        return before - len(self._grants)


def folder_you_named(said: str, target: Path, protected: list[Path]) -> tuple[Path, bool] | None:
    """The folder (or file) in your own words that contains ``target``: (path, file_only).

    The most specific one wins. None when you named nothing that contains it, or what you named
    may not be handed over at all (your home folder, a system folder).
    """
    best: tuple[Path, bool] | None = None
    for named in named_paths(said):
        is_file = named.is_file()
        contains = named == target if is_file else is_within(target, named)
        if not contains:
            continue
        try:
            check_grant(named, protected)
        except Refused:
            continue
        if best is None or len(named.parts) > len(best[0].parts):
            best = (named, is_file)
    return best


def protected_roots(config: Any) -> list[Path]:
    """Folders Bob may never change: its own data, code and plugins, and your protected roots."""
    roots = [config.data_dir, config.plugins_dir, config.knowledge_root]
    roots += [Path(root) for root in config.security.protected_roots]
    if (user_site := getattr(config, "user_site_dir", None)) is not None:
        roots.append(user_site)
    roots += [
        Path(item) for item in os.environ.get("BOB_PROTECTED_ROOTS", "").split(os.pathsep) if item
    ]
    return [Path(os.path.expanduser(str(root))).resolve() for root in roots]
