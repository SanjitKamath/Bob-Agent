from __future__ import annotations

import pytest
from bob_sdk.runtime import ToolError, check_declared
from bob_system import macos, server


def test_only_plain_web_links_are_opened():
    assert macos.check_url(" https://example.com/a?b=1 ") == "https://example.com/a?b=1"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ssh://host", "https://", "x",
                "https://user:pw@example.com/", "shortcuts://run-shortcut?name=x"):  # fmt: skip
        with pytest.raises(ToolError):
            macos.check_url(bad)


def test_notification_text_is_passed_as_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(macos, "_run", lambda command, **kw: calls.append(command) or "")
    macos.notify('Hi" & do shell script "id', "body")
    command = calls[0]
    assert command[0] == "osascript" and command[2] == macos._NOTIFY_SCRIPT
    assert command[3:] == ['Hi" & do shell script "id', "body"]  # data, not script
    with pytest.raises(ToolError):
        macos.notify(" ", "body")


def test_clipboard_limits(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        macos, "_run", lambda command, stdin=None, **kw: sent.update(text=stdin) or "x" * 50_000
    )
    assert macos.clipboard_set("hello") == 5 and sent["text"] == "hello"
    assert len(macos.clipboard_get()) == macos.MAX_CLIPBOARD_CHARS
    with pytest.raises(ToolError):
        macos.clipboard_set("x" * (macos.MAX_CLIPBOARD_CHARS + 1))


def test_server_declares_exactly_its_tools():
    check_declared(server.server, server.MANIFEST)
