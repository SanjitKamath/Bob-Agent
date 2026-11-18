"""Confirm cards: tier-external tool calls wait here for your decision."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from typing import Any

from .db import utcnow
from .events import EventBus


@dataclass
class Confirmation:
    id: str
    plugin: str
    tool: str
    arguments: dict[str, Any]
    actor: str
    session: str | None
    created_at: str = field(default_factory=utcnow)
    _future: asyncio.Future[bool] | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plugin": self.plugin,
            "tool": self.tool,
            "arguments": self.arguments,
            "actor": self.actor,
            "session": self.session,
            "created_at": self.created_at,
        }


class ConfirmationHub:
    def __init__(self, events: EventBus, *, timeout_s: float = 300.0) -> None:
        self._events = events
        self._timeout = timeout_s
        self._pending: dict[str, Confirmation] = {}

    def pending(self) -> list[Confirmation]:
        return list(self._pending.values())

    async def request(
        self,
        *,
        plugin: str,
        tool: str,
        arguments: dict[str, Any],
        actor: str,
        session: str | None,
    ) -> bool:
        """Show a confirm card and wait. No answer within the timeout means no."""
        confirmation = Confirmation(
            id=secrets.token_urlsafe(9),
            plugin=plugin,
            tool=tool,
            arguments=arguments,
            actor=actor,
            session=session,
        )
        confirmation._future = asyncio.get_running_loop().create_future()
        self._pending[confirmation.id] = confirmation
        await self._events.publish("confirm.requested", **confirmation.as_dict())
        try:
            return await asyncio.wait_for(confirmation._future, self._timeout)
        except TimeoutError:
            return False
        finally:
            self._pending.pop(confirmation.id, None)
            await self._events.publish("confirm.closed", id=confirmation.id)

    def resolve(self, confirmation_id: str, approved: bool) -> bool:
        """Record your decision. Returns False if the card is gone (answered or expired)."""
        confirmation = self._pending.get(confirmation_id)
        if confirmation is None or confirmation._future is None or confirmation._future.done():
            return False
        confirmation._future.set_result(approved)
        return True
