"""Thin wrappers over macOS command-line tools."""

from __future__ import annotations

import shutil
import subprocess
from urllib.parse import urlparse

from bob_sdk.runtime import ToolError

MAX_CLIPBOARD_CHARS = 20_000

# The text is passed as arguments, never spliced into the script, so it cannot run as code.
_NOTIFY_SCRIPT = (
    "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
)


def _run(command: list[str], *, stdin: str | None = None, timeout: float = 15.0) -> str:
    binary = shutil.which(command[0])
    if binary is None:
        raise ToolError(f"{command[0]} is not available on this machine")
    try:
        done = subprocess.run(  # noqa: S603 - fixed binaries; user text only as arguments or stdin
            [binary, *command[1:]],
            input=stdin, capture_output=True, text=True, timeout=timeout, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"{command[0]} timed out") from exc
    if done.returncode != 0:
        raise ToolError(done.stderr.strip() or f"{command[0]} failed")
    return done.stdout


def notify(title: str, message: str) -> None:
    title, message = title.strip()[:120], message.strip()[:400]
    if not title or not message:
        raise ToolError("a notification needs a title and a message")
    _run(["osascript", "-e", _NOTIFY_SCRIPT, title, message])


def clipboard_get() -> str:
    return _run(["pbpaste"])[:MAX_CLIPBOARD_CHARS]


def clipboard_set(text: str) -> int:
    if len(text) > MAX_CLIPBOARD_CHARS:
        raise ToolError(f"text is longer than {MAX_CLIPBOARD_CHARS} characters")
    _run(["pbcopy"], stdin=text)
    return len(text)


def check_url(url: str) -> str:
    """Only web links: no file:, no app schemes, no credentials in the URL."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError("only http and https links can be opened")
    if parsed.username or parsed.password:
        raise ToolError("links with embedded credentials are not opened")
    return parsed.geturl()


def open_url(url: str) -> str:
    checked = check_url(url)
    _run(["open", checked])
    return checked
