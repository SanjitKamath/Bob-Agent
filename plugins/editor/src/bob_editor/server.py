"""MCP server for the Editor plugin.

Every tool here is a granted-tier tool: the core only lets a call through when its path is
inside a folder the user named in the chat and approved, and it fills in ``session`` itself.
This server checks the hard rules again and keeps the journal that makes undo possible.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from bob_sdk.runtime import PluginContext, create_server, manifest_for, run

from .journal import Journal
from .ops import Editor

MANIFEST = manifest_for(__file__)
server = create_server(MANIFEST)
_editor: Editor | None = None


def editor() -> Editor:
    global _editor
    if _editor is None:
        ctx = PluginContext.load(MANIFEST)
        protected = [
            Path(item)
            for item in os.environ.get("BOB_PROTECTED_ROOTS", "").split(os.pathsep)
            if item
        ]
        _editor = Editor(Journal(ctx.data_dir), protected)
    return _editor


@server.tool()
def list_dir(path: str, session: str = "") -> dict[str, Any]:
    """List the files and folders in a folder the user named."""
    return editor().list_dir(path)


@server.tool()
def read_file(path: str, max_chars: int = 50_000, session: str = "") -> dict[str, Any]:
    """Read a text file inside a folder the user named."""
    return editor().read_file(path, max_chars)


@server.tool()
def find(path: str, name: str = "", text: str = "", session: str = "") -> dict[str, Any]:
    """Find files by name pattern (like *.md) and/or text inside them, under a folder."""
    return editor().find(path, name, text)


@server.tool()
def history(session: str = "") -> dict[str, Any]:
    """The changes Bob made in this chat, newest first, and which were undone."""
    return editor().history(session)


@server.tool()
def write_file(path: str, content: str, session: str = "") -> dict[str, Any]:
    """Create a file, or replace one completely. The old version is kept for undo."""
    return editor().write_file(path, content, session)


@server.tool()
def edit_file(
    path: str, find: str, replace: str, replace_all: bool = False, session: str = ""
) -> dict[str, Any]:
    """Replace exact text in a file. Read the file first and copy the text exactly."""
    return editor().edit_file(path, find, replace, session, replace_all)


@server.tool()
def make_dir(path: str, session: str = "") -> dict[str, Any]:
    """Create a new folder."""
    return editor().make_dir(path, session)


@server.tool()
def move(source: str, destination: str, session: str = "") -> dict[str, Any]:
    """Move or rename a file or folder. Nothing at the destination is overwritten."""
    return editor().move(source, destination, session)


@server.tool()
def delete(path: str, session: str = "") -> dict[str, Any]:
    """Delete a file or folder. It is kept aside, so undo brings it back."""
    return editor().delete(path, session)


@server.tool()
def undo(steps: int = 1, session: str = "") -> dict[str, Any]:
    """Undo the last change(s) Bob made in this chat, newest first. Stops if a file changed since."""
    return editor().undo(session, steps)


def main() -> None:
    run(server, MANIFEST)


if __name__ == "__main__":
    main()
