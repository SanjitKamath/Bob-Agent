"""Read-only file tools: Spotlight search, grep, reading and listing."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bob_sdk.paths import ReadGuard, is_secret
from bob_sdk.runtime import ToolError

MAX_READ_BYTES = 200_000
MAX_GREP_FILE_BYTES = 2_000_000
MAX_GREP_FILES = 20_000
SKIPPED_FOLDERS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "build", "dist"}


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


class Finder:
    def __init__(self, guard: ReadGuard) -> None:
        self.guard = guard

    def spotlight(self, query: str, limit: int = 20) -> dict[str, Any]:
        """Search file names and contents with Spotlight, inside the readable folders."""
        query = query.strip()
        if not query:
            raise ToolError("a search query is required")
        binary = shutil.which("mdfind")
        if binary is None:
            raise ToolError("Spotlight (mdfind) is not available on this machine")
        limit = max(1, min(limit, 100))
        found: list[str] = []
        for root in self.guard.roots:
            if not root.is_dir():
                continue
            try:
                done = subprocess.run(  # noqa: S603 - fixed binary; the query is one argument
                    [binary, "-onlyin", str(root), query],
                    capture_output=True, text=True, timeout=20, check=False,
                )  # fmt: skip
            except subprocess.TimeoutExpired:
                continue
            for line in done.stdout.splitlines():
                path = Path(line)
                if line and line not in found and self.guard.allows(path):
                    found.append(line)
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
        return {"query": query, "count": len(found), "paths": found}

    def grep(
        self, pattern: str, path: str, ignore_case: bool = True, limit: int = 50
    ) -> dict[str, Any]:
        """Search file contents under a folder (or one file) with a regular expression."""
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc
        root = self.guard.check(path)
        limit = max(1, min(limit, 200))
        matches: list[dict[str, Any]] = []
        scanned = 0
        truncated = False
        for file in self._walk(root):
            scanned += 1
            if scanned > MAX_GREP_FILES:
                truncated = True
                break
            try:
                if file.stat().st_size > MAX_GREP_FILE_BYTES:
                    continue
                data = file.read_bytes()
            except OSError:
                continue
            if _looks_binary(data[:4096]):
                continue
            for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
                if regex.search(line):
                    matches.append({"path": str(file), "line": number, "text": line.strip()[:300]})
                    if len(matches) >= limit:
                        return {"pattern": pattern, "matches": matches, "truncated": True}
        return {"pattern": pattern, "matches": matches, "truncated": truncated}

    def _walk(self, root: Path):
        if root.is_file():
            yield root
            return
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if d not in SKIPPED_FOLDERS and not is_secret(Path(folder) / d)
            )
            for name in sorted(files):
                file = Path(folder) / name
                if not file.is_symlink() and not is_secret(file):
                    yield file

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 400) -> dict[str, Any]:
        """Read a text file, or a range of its lines."""
        file = self.guard.check(path)
        if not file.is_file():
            raise ToolError(f"{file} is not a file")
        with open(file, "rb") as handle:
            data = handle.read(MAX_READ_BYTES + 1)
        if _looks_binary(data[:4096]):
            raise ToolError(f"{file} is not a text file; use a documents tool for PDF or DOCX")
        clipped = len(data) > MAX_READ_BYTES
        lines = data[:MAX_READ_BYTES].decode("utf-8", "replace").splitlines()
        start = max(1, start_line)
        count = max(1, min(max_lines, 2000))
        window = lines[start - 1 : start - 1 + count]
        return {
            "path": str(file),
            "start_line": start,
            "end_line": start + len(window) - 1 if window else start - 1,
            "total_lines": len(lines),
            "truncated": clipped or start - 1 + count < len(lines),
            "text": "\n".join(window),
        }

    def list_dir(self, path: str, limit: int = 200) -> dict[str, Any]:
        """List a folder's entries with type and size."""
        folder = self.guard.check(path)
        if not folder.is_dir():
            raise ToolError(f"{folder} is not a folder")
        entries: list[dict[str, Any]] = []
        names = sorted(os.listdir(folder))
        for name in names:
            item = folder / name
            if is_secret(item):
                continue
            try:
                stat = item.lstat()
            except OSError:
                continue
            kind = "folder" if item.is_dir() else "link" if item.is_symlink() else "file"
            entries.append(
                {"name": name, "type": kind, "size": stat.st_size if kind == "file" else None}
            )
            if len(entries) >= max(1, min(limit, 1000)):
                break
        return {"path": str(folder), "entries": entries, "truncated": len(entries) < len(names)}

    def reveal_in_finder(self, path: str) -> dict[str, Any]:
        """Show a file or folder in the Finder."""
        target = self.guard.check(path)
        binary = shutil.which("open")
        if binary is None:
            raise ToolError("cannot open the Finder on this machine")
        done = subprocess.run(  # noqa: S603 - fixed binary; -R only reveals
            [binary, "-R", str(target)], capture_output=True, text=True, timeout=15, check=False
        )
        if done.returncode != 0:
            raise ToolError(done.stderr.strip() or "the Finder could not reveal the path")
        return {"revealed": str(target)}
