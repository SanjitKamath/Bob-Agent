"""MCP server for the System plugin."""

from __future__ import annotations

from typing import Any

from bob_sdk.runtime import create_server, manifest_for, run

from . import macos

MANIFEST = manifest_for(__file__)
server = create_server(MANIFEST)


@server.tool()
def notify(title: str, message: str) -> dict[str, Any]:
    """Show a macOS notification."""
    macos.notify(title, message)
    return {"ok": True}


@server.tool()
def clipboard_get() -> dict[str, Any]:
    """Read the text on the clipboard."""
    return {"text": macos.clipboard_get()}


@server.tool()
def clipboard_set(text: str) -> dict[str, Any]:
    """Put text on the clipboard."""
    return {"ok": True, "characters": macos.clipboard_set(text)}


@server.tool()
def open_url(url: str) -> dict[str, Any]:
    """Open an http or https link in the default browser."""
    return {"opened": macos.open_url(url)}


def main() -> None:
    run(server, MANIFEST)


if __name__ == "__main__":
    main()
