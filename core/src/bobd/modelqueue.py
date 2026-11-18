"""The model scheduler: how many requests each model runs at once, and who goes first.

Every request bobd sends to Ollama for a chat, a completion or embeddings passes through
one queue per model. A model runs at most its limit of requests at once (set in Settings →
Models). When it is full, a chat you are waiting on goes ahead of background work such as
a form build, mail triage or a routine. Requests of the same priority keep their order.

The queue sits in bobd's HTTP connection to Ollama, so every caller in bobd (the agent,
the router, quarantined summaries, plugins' generate calls) is scheduled the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx

CHAT = 0  # someone is waiting for this answer
BACKGROUND = 1  # routines, jobs, plugins working on their own

_PRIORITY: ContextVar[int] = ContextVar("bob_model_priority", default=BACKGROUND)

# Ollama endpoints that run a model; anything else (listing, pulling) is not queued.
QUEUED_PATHS = ("/api/chat", "/api/generate", "/api/embed", "/api/embeddings",
                "/v1/chat/completions", "/v1/completions", "/v1/embeddings")  # fmt: skip

DEFAULT_LIMITS = {"router": 4, "main": 2, "coder": 2, "vision": 2, "embeddings": 4}
MIN_LIMIT, MAX_LIMIT = 1, 8


@contextlib.contextmanager
def priority(level: int) -> Iterator[None]:
    """Model requests made inside this block (and tasks started from it) use ``level``."""
    token = _PRIORITY.set(level)
    try:
        yield
    finally:
        _PRIORITY.reset(token)


def current_priority() -> int:
    return _PRIORITY.get()


@dataclass
class _Slots:
    running: int = 0
    waiting: list[tuple[int, int, asyncio.Future[None]]] = field(default_factory=list)
    served: int = 0

    def wake(self, limit: int) -> None:
        while self.waiting and self.running < limit:
            _level, _seq, future = heapq.heappop(self.waiting)
            if future.done():  # given up while waiting
                continue
            self.running += 1
            future.set_result(None)


class ModelQueue:
    """Per-model limits with priority. ``resolve(model)`` gives (queue name, limit)."""

    def __init__(self, resolve: Callable[[str], tuple[str, int]]) -> None:
        self._resolve = resolve
        self._slots: dict[str, _Slots] = {}
        self._seq = itertools.count()

    def _limit(self, name: str) -> int:
        return max(MIN_LIMIT, min(MAX_LIMIT, self._resolve(name)[1]))

    @contextlib.asynccontextmanager
    async def slot(self, model: str, level: int | None = None) -> AsyncIterator[None]:
        name, _ = self._resolve(model)
        slots = self._slots.setdefault(name, _Slots())
        level = current_priority() if level is None else level
        limit = self._limit(name)
        if slots.running < limit and not slots.waiting:
            slots.running += 1
        else:
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            heapq.heappush(slots.waiting, (level, next(self._seq), future))
            try:
                await future
            except asyncio.CancelledError:
                if future.done() and not future.cancelled():
                    slots.running -= 1  # it got a slot just as it was cancelled
                    slots.wake(self._limit(name))
                else:
                    future.cancel()
                raise
        try:
            yield
        finally:
            slots.running -= 1
            slots.served += 1
            slots.wake(self._limit(name))

    def wake_all(self) -> None:
        """Let waiting requests start after a limit was raised."""
        for name, slots in self._slots.items():
            slots.wake(self._limit(name))

    def status(self) -> dict[str, dict[str, int]]:
        return {
            name: {
                "running": slots.running,
                "waiting": sum(1 for *_, future in slots.waiting if not future.done()),
                "waiting_chats": sum(
                    1 for level, _, future in slots.waiting if level == CHAT and not future.done()
                ),
                "served": slots.served,
                "limit": self._limit(name),
            }
            for name, slots in self._slots.items()
        }


class _Releasing(httpx.AsyncByteStream):
    """A response body that frees the model's slot once it has been read or closed."""

    def __init__(self, inner: httpx.AsyncByteStream, release: Callable[[], Any]) -> None:
        self._inner = inner
        self._release = release
        self._released = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            yield chunk

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            if not self._released:
                self._released = True
                await self._release()


class QueuedTransport(httpx.AsyncBaseTransport):
    """Holds a model slot from sending a request to Ollama until its answer is read."""

    def __init__(self, inner: httpx.AsyncBaseTransport, queue: ModelQueue) -> None:
        self._inner = inner
        self._queue = queue

    @staticmethod
    async def _model_of(request: httpx.Request) -> str | None:
        if request.method != "POST" or not request.url.path.endswith(QUEUED_PATHS):
            return None
        try:
            body = json.loads(await request.aread() or b"{}")
        except ValueError:
            return None
        model = body.get("model") if isinstance(body, dict) else None
        return model if isinstance(model, str) and model else None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        model = await self._model_of(request)
        if model is None:
            return await self._inner.handle_async_request(request)
        held = self._queue.slot(model)
        await held.__aenter__()
        try:
            response = await self._inner.handle_async_request(request)
        except BaseException as exc:
            await held.__aexit__(type(exc), exc, exc.__traceback__)
            raise

        async def release() -> None:
            await held.__aexit__(None, None, None)

        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_Releasing(response.stream, release),  # type: ignore[arg-type]
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()
