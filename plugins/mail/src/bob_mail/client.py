"""Talking to the mail server: IMAP for reading and drafts, SMTP for sending."""

from __future__ import annotations

import email
import email.policy
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from bob_sdk.runtime import ToolError

MAX_BODY_CHARS = 20_000


@dataclass(frozen=True)
class RawMessage:
    """One message as fetched. The body is untrusted and never leaves the plugin raw."""

    uid: int
    message_id: str
    references: tuple[str, ...]
    sender: str
    recipients: str
    subject: str
    date: str  # ISO 8601, UTC
    body: str


def parse_message(uid: int, data: bytes) -> RawMessage:
    message = email.message_from_bytes(data, policy=email.policy.default)
    part = message.get_body(preferencelist=("plain", "html"))
    body = ""
    if part is not None:
        try:
            body = part.get_content()
        except (LookupError, UnicodeError):  # unknown or broken charset
            body = part.get_payload(decode=True).decode("utf-8", "replace")  # type: ignore[union-attr]
        if part.get_content_type() == "text/html":
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(body, "html.parser")
            for tag in soup(["script", "style", "head"]):
                tag.decompose()
            body = soup.get_text("\n", strip=True)
    try:
        when = parsedate_to_datetime(str(message.get("Date")))
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        date = when.astimezone(UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        date = datetime.now(UTC).isoformat(timespec="seconds")
    references = tuple(str(message.get("References", "")).split())
    in_reply_to = str(message.get("In-Reply-To", "")).strip()
    if in_reply_to and in_reply_to not in references:
        references = (*references, in_reply_to)
    return RawMessage(
        uid=uid,
        message_id=str(message.get("Message-ID", f"<uid-{uid}@local>")).strip(),
        references=references,
        sender=str(message.get("From", "")).strip()[:300],
        recipients=str(message.get("To", "")).strip()[:500],
        subject=" ".join(str(message.get("Subject", "(no subject)")).split())[:300],
        date=date,
        body=body[:MAX_BODY_CHARS],
    )


def build_message(
    *, sender: str, to: str, subject: str, body: str, in_reply_to: str | None = None,
    references: tuple[str, ...] = (),
) -> EmailMessage:  # fmt: skip
    for value in (sender, to, subject):
        if "\n" in value or "\r" in value:
            raise ToolError("mail headers cannot contain line breaks")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, to, subject
    message["Date"] = email.utils.format_datetime(datetime.now(UTC))
    message["Message-ID"] = email.utils.make_msgid()
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = " ".join(dict.fromkeys([*references, in_reply_to]))
    message.set_content(body)
    return message


class MailClient(Protocol):
    def fetch_since(self, last_uid: int, limit: int) -> list[RawMessage]: ...
    def search(self, text: str, limit: int) -> list[RawMessage]: ...
    def save_draft(self, message: EmailMessage) -> None: ...
    def send(self, message: EmailMessage) -> None: ...
    def archive(self, uid: int) -> None: ...
    def delete(self, uid: int) -> None: ...


@dataclass(frozen=True)
class Account:
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    username: str
    password: str
    inbox: str = "INBOX"


class ImapSmtpClient:
    """IMAP over TLS with ``imapclient``; SMTP over TLS with ``smtplib``.

    Each operation opens its own connection. That keeps the client stateless
    across the long gaps between triage runs.
    """

    def __init__(self, account: Account) -> None:
        self._account = account

    def _imap(self):
        from imapclient import IMAPClient

        try:
            client = IMAPClient(
                self._account.imap_host, port=self._account.imap_port, ssl=True, timeout=60
            )
            client.login(self._account.username, self._account.password)
        except Exception as exc:  # imapclient raises several unrelated error types
            raise ToolError(f"cannot sign in to the mail server: {exc.__class__.__name__}") from exc
        return client

    def _fetch(self, client, uids: list[int]) -> list[RawMessage]:
        if not uids:
            return []
        # BODY.PEEK[] reads without marking the message as seen.
        fetched = client.fetch(uids, ["BODY.PEEK[]"])
        messages = []
        for uid in sorted(fetched):
            data = fetched[uid].get(b"BODY[]")
            if data:
                messages.append(parse_message(int(uid), data))
        return messages

    def fetch_since(self, last_uid: int, limit: int) -> list[RawMessage]:
        with self._imap() as client:
            client.select_folder(self._account.inbox, readonly=True)
            uids = [uid for uid in client.search(["UID", f"{last_uid + 1}:*"]) if uid > last_uid]
            return self._fetch(client, sorted(uids)[-limit:])

    def search(self, text: str, limit: int) -> list[RawMessage]:
        with self._imap() as client:
            client.select_folder(self._account.inbox, readonly=True)
            uids = client.search(["TEXT", text], charset="UTF-8")
            return self._fetch(client, sorted(uids)[-limit:])

    def _special(self, client, flag: bytes, fallback: str) -> str:
        return client.find_special_folder(flag) or fallback

    def save_draft(self, message: EmailMessage) -> None:
        import imapclient

        with self._imap() as client:
            folder = self._special(client, imapclient.DRAFTS, "Drafts")
            client.append(folder, message.as_bytes(), flags=[imapclient.DRAFT])

    def _move(self, uid: int, flag: bytes, fallback: str) -> None:
        with self._imap() as client:
            client.select_folder(self._account.inbox)
            folder = self._special(client, flag, fallback)
            if client.has_capability("MOVE"):
                client.move([uid], folder)
            else:
                import imapclient

                client.copy([uid], folder)
                client.add_flags([uid], [imapclient.DELETED])
                client.expunge([uid])

    def archive(self, uid: int) -> None:
        import imapclient

        self._move(uid, imapclient.ARCHIVE, "Archive")

    def delete(self, uid: int) -> None:
        import imapclient

        # Moved to the Trash, so the mail server's own retention still applies.
        self._move(uid, imapclient.TRASH, "Trash")

    def _smtp(self):
        """Signed in to SMTP: TLS from the start on 465, STARTTLS on 587 and others."""
        context = ssl.create_default_context()
        host, port = self._account.smtp_host, self._account.smtp_port
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, context=context, timeout=60)
        else:
            smtp = smtplib.SMTP(host, port, timeout=60)
            smtp.starttls(context=context)
        smtp.login(self._account.username, self._account.password)
        return smtp

    def send(self, message: EmailMessage) -> None:
        try:
            with self._smtp() as smtp:
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise ToolError(f"the mail server refused to send: {exc.__class__.__name__}") from exc

    def check(self) -> dict[str, Any]:
        """Sign in to both servers and report what happened. Reads and sends nothing."""
        result: dict[str, Any] = {"ok": False, "imap": "", "smtp": "", "error": ""}
        try:
            client = self._imap()
            try:
                folder = client.select_folder(self._account.inbox, readonly=True)
                count = folder.get(b"EXISTS", 0)
                result["imap"] = f"signed in; {self._account.inbox} has {count} message(s)"
            finally:
                client.logout()
        except Exception as exc:  # a ToolError from _imap, or the folder is missing
            result["imap"] = f"failed: {exc}"
        try:
            with self._smtp():
                result["smtp"] = "signed in"
        except smtplib.SMTPAuthenticationError:
            result["smtp"] = "failed: the server rejected the username or app password"
        except (smtplib.SMTPException, OSError) as exc:
            result["smtp"] = f"failed: {exc.__class__.__name__}: {exc}"
        result["ok"] = result["imap"].startswith("signed in") and result["smtp"] == "signed in"
        if not result["ok"]:
            result["error"] = "; ".join(
                f"{name}: {value}"
                for name, value in (("IMAP", result["imap"]), ("SMTP", result["smtp"]))
                if not value.startswith("signed in")
            )
        return result
