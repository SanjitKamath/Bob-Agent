"""MCP server for the Files plugin. Every tool is read-only."""

from __future__ import annotations

from typing import Any

from bob_sdk.paths import ReadGuard
from bob_sdk.runtime import PluginContext, create_server, manifest_for, run

from .finder import Finder

MANIFEST = manifest_for(__file__)
server = create_server(MANIFEST)
_finder: Finder | None = None


def finder() -> Finder:
    global _finder
    if _finder is None:
        ctx = PluginContext.load(MANIFEST)
        _finder = Finder(ReadGuard(ctx.setting("read_roots")))
    return _finder


@server.tool()
def spotlight(query: str, limit: int = 20) -> dict[str, Any]:
    """Find files by name or content with Spotlight."""
    return finder().spotlight(query, limit)


@server.tool()
def grep(pattern: str, path: str, ignore_case: bool = True, limit: int = 50) -> dict[str, Any]:
    """Search text files under a folder with a regular expression. Returns path, line, text."""
    return finder().grep(pattern, path, ignore_case, limit)


@server.tool()
def read_file(path: str, start_line: int = 1, max_lines: int = 400) -> dict[str, Any]:
    """Read a text file, optionally from a given line."""
    return finder().read_file(path, start_line, max_lines)


@server.tool()
def list_dir(path: str, limit: int = 200) -> dict[str, Any]:
    """List the entries of a folder."""
    return finder().list_dir(path, limit)


@server.tool()
def reveal_in_finder(path: str) -> dict[str, Any]:
    """Show a file or folder in the Finder."""
    return finder().reveal_in_finder(path)


def main() -> None:
    run(server, MANIFEST)


if __name__ == "__main__":
    main()
