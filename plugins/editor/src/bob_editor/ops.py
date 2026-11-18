"""Reading and changing files inside the folders you named, one undoable step at a time."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from bob_sdk.paths import is_secret
from bob_sdk.runtime import ToolError
from bob_sdk.writeguard import Refused, check_read, check_write

from .journal import Journal

MAX_READ_CHARS = 200_000
MAX_WRITE_BYTES = 2_000_000
MAX_LISTED = 500
MAX_FOUND = 200
MAX_TREE_FILES = 2_000
MAX_TREE_BYTES = 500_000_000
MAX_SEARCHED_BYTES = 2_000_000


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_text(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" not in handle.read(8192)
    except OSError:
        return False


class Editor:
    def __init__(self, journal: Journal, protected: list[Path]) -> None:
        self.journal = journal
        self.protected = [*protected, journal.backups.parent]

    # -- guards -----------------------------------------------------------------------------

    def _read(self, path: str) -> Path:
        try:
            return check_read(path)
        except Refused as exc:
            raise ToolError(str(exc)) from exc

    def _write(self, path: str | Path) -> Path:
        try:
            return check_write(path, self.protected)
        except Refused as exc:
            raise ToolError(str(exc)) from exc

    @staticmethod
    def _need_session(session: str) -> str:
        if not session:
            raise ToolError("changes are only made inside a chat, so they can be undone")
        return session

    # -- reading ----------------------------------------------------------------------------

    def list_dir(self, path: str) -> dict[str, Any]:
        folder = self._read(path)
        if not folder.is_dir():
            raise ToolError(f"{folder} is not a folder")
        entries = []
        for child in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if is_secret(child):
                continue
            entries.append(
                {
                    "name": child.name,
                    "type": "folder" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else None,
                }
            )
        return {
            "path": str(folder),
            "entries": entries[:MAX_LISTED],
            "truncated": len(entries) > MAX_LISTED,
        }

    def read_file(self, path: str, max_chars: int = 50_000) -> dict[str, Any]:
        file = self._read(path)
        if not file.is_file():
            raise ToolError(f"{file} is not a file")
        if not _is_text(file):
            raise ToolError(f"{file} is not a text file")
        limit = max(1, min(max_chars, MAX_READ_CHARS))
        text = file.read_text(encoding="utf-8", errors="replace")
        return {
            "path": str(file),
            "text": text[:limit],
            "truncated": len(text) > limit,
            "chars": len(text),
        }

    def find(self, path: str, name: str = "", text: str = "") -> dict[str, Any]:
        root = self._read(path)
        if not root.is_dir():
            raise ToolError(f"{root} is not a folder")
        if not name and not text:
            raise ToolError("give a file name pattern, text to look for, or both")
        found: list[dict[str, Any]] = []
        for base, folders, files in os.walk(root):
            folders[:] = sorted(f for f in folders if not is_secret(Path(base) / f))
            for file in sorted(files):
                candidate = Path(base) / file
                if is_secret(candidate) or (
                    name and not fnmatch.fnmatch(file.lower(), name.lower())
                ):
                    continue
                hit: dict[str, Any] = {"path": str(candidate)}
                if text:
                    if not candidate.is_file() or candidate.stat().st_size > MAX_SEARCHED_BYTES:
                        continue
                    if not _is_text(candidate):
                        continue
                    lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
                    matches = [
                        {"line": n, "text": line.strip()[:200]}
                        for n, line in enumerate(lines, 1)
                        if text.lower() in line.lower()
                    ]
                    if not matches:
                        continue
                    hit["matches"] = matches[:5]
                found.append(hit)
                if len(found) >= MAX_FOUND:
                    return {"found": found, "truncated": True}
        return {"found": found, "truncated": False}

    def history(self, session: str) -> dict[str, Any]:
        return {"changes": self.journal.entries(session)}

    # -- changing ---------------------------------------------------------------------------

    def write_file(self, path: str, content: str, session: str) -> dict[str, Any]:
        self._need_session(session)
        file = self._write(path)
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError("that is too large to write in one go")
        if file.is_dir():
            raise ToolError(f"{file} is a folder")
        if not file.parent.is_dir():
            raise ToolError(f"{file.parent} does not exist; make the folder first")
        existed = file.exists()
        backup = self._keep(file) if existed else None
        file.write_bytes(data)
        self.journal.record(
            session, "write", file, existed=existed, backup=backup, after_sha=_sha(file)
        )
        return {"path": str(file), "created": not existed, "bytes": len(data)}

    def edit_file(
        self, path: str, find: str, replace: str, session: str, replace_all: bool = False
    ) -> dict[str, Any]:
        self._need_session(session)
        file = self._write(path)
        if not file.is_file() or not _is_text(file):
            raise ToolError(f"{file} is not a text file")
        if not find:
            raise ToolError("say which text to replace")
        text = file.read_text(encoding="utf-8")
        count = text.count(find)
        if count == 0:
            raise ToolError("that text is not in the file; read the file again and copy it exactly")
        if count > 1 and not replace_all:
            raise ToolError(
                f"that text appears {count} times; include more around it or set replace_all"
            )
        updated = text.replace(find, replace) if replace_all else text.replace(find, replace, 1)
        if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolError("the result would be too large")
        backup = self._keep(file)
        file.write_text(updated, encoding="utf-8")
        self.journal.record(
            session, "edit", file, existed=True, backup=backup, after_sha=_sha(file)
        )
        return {"path": str(file), "replaced": count if replace_all else 1}

    def make_dir(self, path: str, session: str) -> dict[str, Any]:
        self._need_session(session)
        folder = self._write(path)
        if folder.exists():
            raise ToolError(f"{folder} already exists")
        if not folder.parent.is_dir():
            raise ToolError(f"{folder.parent} does not exist")
        folder.mkdir()
        self.journal.record(session, "mkdir", folder, existed=False)
        return {"path": str(folder)}

    def move(self, source: str, destination: str, session: str) -> dict[str, Any]:
        self._need_session(session)
        src, dst = self._write(source), self._write(destination)
        if not src.exists():
            raise ToolError(f"{src} does not exist")
        if dst.exists():
            raise ToolError(f"{dst} already exists; nothing is overwritten by a move")
        if not dst.parent.is_dir():
            raise ToolError(f"{dst.parent} does not exist")
        if dst == src or dst.is_relative_to(src):
            raise ToolError("cannot move something into itself")
        shutil.move(str(src), str(dst))
        self.journal.record(session, "move", src, dest=dst, existed=True)
        return {"from": str(src), "to": str(dst)}

    def delete(self, path: str, session: str) -> dict[str, Any]:
        """Move to the journal's keeping place, from where undo can bring it back."""
        self._need_session(session)
        target = self._write(path)
        if not target.exists() and not target.is_symlink():
            raise ToolError(f"{target} does not exist")
        files, size = self._measure(target)
        if files > MAX_TREE_FILES or size > MAX_TREE_BYTES:
            raise ToolError("that is too much to delete in one step; do it in smaller parts")
        slot = self.journal.backup_slot()
        kept = slot / "item"
        shutil.move(str(target), str(kept))
        self.journal.record(session, "delete", target, existed=True, backup=kept)
        return {"deleted": str(target), "files": files}

    # -- undo -------------------------------------------------------------------------------

    def undo(self, session: str, steps: int = 1) -> dict[str, Any]:
        self._need_session(session)
        steps = max(1, min(steps, 20))
        undone: list[dict[str, Any]] = []
        for row in self.journal.pending(session, steps):
            try:
                self._undo_one(row)
            except ToolError as exc:
                return {"undone": undone, "stopped_because": str(exc)}
            self.journal.mark_undone(row["id"])
            undone.append({"id": row["id"], "op": row["op"], "path": row["path"]})
        if not undone:
            return {"undone": [], "stopped_because": "there is nothing left to undo in this chat"}
        return {"undone": undone}

    def _undo_one(self, row: Any) -> None:
        op, path = row["op"], self._write(row["path"])
        backup = Path(row["backup"]) if row["backup"] else None
        if op in ("write", "edit"):
            if not path.is_file() or _sha(path) != row["after_sha"]:
                raise ToolError(
                    f"{path} has been changed since Bob changed it, so undoing would lose your "
                    "newer edits; nothing more was undone"
                )
            if row["existed"]:
                if backup is None or not backup.is_file():
                    raise ToolError(f"the original of {path} is no longer kept")
                shutil.copy2(backup, path)
            else:
                path.unlink()
        elif op == "mkdir":
            if not path.is_dir() or any(path.iterdir()):
                raise ToolError(f"{path} is no longer an empty folder; leaving it as it is")
            path.rmdir()
        elif op == "move":
            dest = self._write(row["dest"])
            if not dest.exists() or path.exists():
                raise ToolError(
                    f"{dest} has moved again or {path} is taken; leaving them as they are"
                )
            shutil.move(str(dest), str(path))
        elif op == "delete":
            if path.exists():
                raise ToolError(f"{path} exists again; leaving it as it is")
            if backup is None or not (backup.exists() or backup.is_symlink()):
                raise ToolError(f"what was deleted at {path} is no longer kept")
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup), str(path))

    # -- helpers ----------------------------------------------------------------------------

    def _keep(self, file: Path) -> Path:
        slot = self.journal.backup_slot()
        kept = slot / "item"
        shutil.copy2(file, kept)
        return kept

    @staticmethod
    def _measure(path: Path) -> tuple[int, int]:
        if path.is_symlink() or path.is_file():
            return 1, path.lstat().st_size
        files = size = 0
        for base, _folders, names in os.walk(path):
            for name in names:
                files += 1
                size += (Path(base) / name).lstat().st_size
        return files, size
