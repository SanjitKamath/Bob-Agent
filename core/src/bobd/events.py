"""A small in-process event bus.

Subsystems publish events (``plugin.enabled``, ``mail.triaged``, ...). The API
forwards them to the app, and event-driven routines subscribe by topic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .db import utcnow

log = logging.getLogger(__name__)

Handler = Callable[["Event"], Awaitable[None]]


@dataclass(frozen=True)
class Event:
    topic: str
    data: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "data": self.data, "at": self.at}


class EventBus:
    def __init__(self, queue_size: int = 256) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._queues: set[asyncio.Queue[Event]] = set()
        self._queue_size = queue_size

    def on(self, topic: str, handler: Handler) -> Callable[[], None]:
        """Call ``handler`` for every event on ``topic``. Returns an unsubscribe function."""
        self._handlers.setdefault(topic, []).append(handler)

        def unsubscribe() -> None:
            handlers = self._handlers.get(topic, [])
            if handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    async def publish(self, topic: str, **data: Any) -> Event:
        event = Event(topic, data)
        for queue in list(self._queues):
            if queue.full():
                # A slow listener loses its oldest event rather than blocking the service.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)
        for handler in list(self._handlers.get(topic, [])):
            try:
                await handler(event)
            except Exception:  # a failing handler must not break the publisher
                log.exception("event handler failed for %s", topic)
        return event

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        """Receive every event through a queue, for streaming to the app."""
        queue: asyncio.Queue[Event] = asyncio.Queue(self._queue_size)
        self._queues.add(queue)
        try:
            yield queue
        finally:
            self._queues.discard(queue)
