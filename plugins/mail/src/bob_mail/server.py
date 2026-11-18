"""MCP server for the Mail plugin."""

from __future__ import annotations

import asyncio
from typing import Any

from bob_sdk.runtime import PluginContext, ToolError, create_server, manifest_for, run

from .client import Account, ImapSmtpClient
from .service import MailService

MANIFEST = manifest_for(__file__)
server = create_server(MANIFEST)

KEYRING_SERVICE = "bob"
KEYRING_PASSWORD = "mail-password"

_service: MailService | None = None


def _password() -> str:
    try:
        import keyring

        password = keyring.get_password(KEYRING_SERVICE, KEYRING_PASSWORD)
    except Exception as exc:  # no usable keychain
        raise ToolError(f"the Keychain is not available: {exc.__class__.__name__}") from exc
    if not password:
        raise ToolError("no mail password in the Keychain; run: bob mail login")
    return password


SETUP_HINT = "run: bob mail setup"
REQUIRED = ("imap_host", "smtp_host", "username")


def missing_settings() -> list[str]:
    settings = PluginContext.load(MANIFEST).settings
    return [key for key in REQUIRED if not settings.get(key)]


def _not_set_up(missing: list[str]) -> dict[str, Any]:
    return {
        "set_up": False,
        "missing": missing,
        "status": f"Not set up: {SETUP_HINT}",
        "counts": {},
    }


def account() -> Account:
    ctx = PluginContext.load(MANIFEST)
    settings = ctx.settings
    missing = [key for key in REQUIRED if not settings.get(key)]
    if missing:
        raise ToolError(f"mail is not set up yet (missing {', '.join(missing)}); {SETUP_HINT}")
    return Account(
        imap_host=settings["imap_host"],
        imap_port=int(settings["imap_port"]),
        smtp_host=settings["smtp_host"],
        smtp_port=int(settings["smtp_port"]),
        username=settings["username"],
        password=_password(),
        inbox=settings.get("inbox") or "INBOX",
    )


def service() -> MailService:
    global _service
    if _service is not None:
        return _service
    ctx = PluginContext.load(MANIFEST)
    settings = ctx.settings
    account_ = account()
    core = ctx.core

    async def create_task(title: str, source: str) -> None:
        await core.call_tool("tasks", "create", {"title": title, "notes": source})

    async def urgent(message: dict[str, Any]) -> None:
        # The app turns this event into a notification with action buttons.
        await core.publish("urgent", message)

    async def best_effort(call, *args: Any) -> None:
        # Tasks may be switched off, or bobd busy; triage still completes.
        try:
            await call(*args)
        except ToolError:
            return

    _service = MailService(
        ImapSmtpClient(account_),
        ctx.data_dir / "mail.sqlite",
        address=account_.username,
        summarise=lambda text: core.summarize_untrusted(text, kind="email"),
        generate=lambda system, prompt: core.generate("main", system, prompt),
        create_task=lambda title, source: best_effort(create_task, title, source),
        notify=lambda message: best_effort(urgent, message),
        auto_draft=bool(settings.get("auto_draft", True)),
        fetch_limit=int(settings.get("fetch_limit") or 50),
        skip_subjects=list(settings.get("skip_subjects") or []),
    )
    return _service


@server.tool()
async def list_new(limit: int = 20) -> dict[str, Any]:
    """Fetch new mail and list the inbox, newest first, as structured summaries."""
    return await service().list_new(limit)


@server.tool()
async def search(query: str, limit: int = 10) -> dict[str, Any]:
    """Search the mailbox for text. Returns structured summaries."""
    return await service().search(query, limit)


@server.tool()
async def get_thread(message_id: int) -> dict[str, Any]:
    """All messages of one conversation, oldest first, as structured summaries."""
    return await service().get_thread(message_id)


@server.tool()
def summary() -> dict[str, Any]:
    """Counts of urgent and needs-reply mail, and when triage last ran."""
    if missing := missing_settings():
        return _not_set_up(missing)
    return service().summary()


@server.tool()
async def triage(limit: int = 25) -> dict[str, Any]:
    """Label new mail, pull out tasks, and draft replies for mail that needs one."""
    if missing := missing_settings():
        # Not an error: there is simply no account yet. The routine records it as skipped.
        return {
            **_not_set_up(missing),
            "skipped": True,
            "reason": f"mail is not set up; {SETUP_HINT}",
        }
    return await service().triage(limit)


@server.tool()
async def check_account() -> dict[str, Any]:
    """Sign in to the mail account's IMAP and SMTP servers without reading or sending mail."""
    try:
        current = account()
    except ToolError as exc:
        return {"ok": False, "imap": "not tried", "smtp": "not tried", "error": str(exc)}
    return await asyncio.to_thread(ImapSmtpClient(current).check)


@server.tool()
async def draft_reply(message_id: int, instructions: str = "") -> dict[str, Any]:
    """Write a reply to a message and save it to Drafts. It is not sent."""
    return await service().draft_reply(message_id, instructions)


@server.tool()
async def send(to: str, subject: str, body: str, in_reply_to: int | None = None) -> dict[str, Any]:
    """Send an email. Always shows a confirm card first."""
    return await service().send(to, subject, body, in_reply_to)


@server.tool()
async def archive(message_id: int) -> dict[str, Any]:
    """Move a message to the Archive. Always shows a confirm card first."""
    return await service().archive(message_id)


@server.tool()
async def delete(message_id: int) -> dict[str, Any]:
    """Move a message to the Trash. Always shows a confirm card first."""
    return await service().delete(message_id)


def main() -> None:
    run(server, MANIFEST)


if __name__ == "__main__":
    main()
