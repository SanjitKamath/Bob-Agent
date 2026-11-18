"""Several chats at once, the model scheduler, and chat history."""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest
from bobd.api import create_api
from bobd.modelqueue import BACKGROUND, CHAT, ModelQueue, QueuedTransport, priority
from fastapi.testclient import TestClient
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import FunctionModel

AUTH = {"Authorization": "Bearer test-token"}


# -- the model scheduler ---------------------------------------------------------------------


async def test_a_full_model_serves_chats_before_background_work():
    queue = ModelQueue(lambda model: (model, 1))
    order: list[str] = []
    release = asyncio.Event()

    async def hold() -> None:
        async with queue.slot("m", BACKGROUND):
            await release.wait()

    async def wait(name: str, level: int) -> None:
        async with queue.slot("m", level):
            order.append(name)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    waiting = [
        asyncio.create_task(wait("form build 1", BACKGROUND)),
        asyncio.create_task(wait("form build 2", BACKGROUND)),
    ]
    await asyncio.sleep(0)
    waiting.append(asyncio.create_task(wait("your chat", CHAT)))
    await asyncio.sleep(0)
    assert queue.status()["m"] == {
        "running": 1, "waiting": 3, "waiting_chats": 1, "served": 0, "limit": 1
    }  # fmt: skip
    release.set()
    await asyncio.gather(holder, *waiting)
    assert order == ["your chat", "form build 1", "form build 2"]
    assert queue.status()["m"]["running"] == 0


async def test_the_limit_is_kept_and_a_cancelled_wait_frees_nothing_it_never_had():
    limit = {"value": 2}
    queue = ModelQueue(lambda model: (model, limit["value"]))
    now = most = 0
    gate = asyncio.Event()

    async def work() -> None:
        nonlocal now, most
        async with queue.slot("m"):
            now += 1
            most = max(most, now)
            await gate.wait()
            now -= 1

    tasks = [asyncio.create_task(work()) for _ in range(5)]
    await asyncio.sleep(0.01)
    assert most == 2
    tasks[4].cancel()  # gave up while waiting
    await asyncio.sleep(0)
    limit["value"] = 3
    queue.wake_all()  # a raised limit lets one more start straight away
    await asyncio.sleep(0)
    assert now == 3
    gate.set()
    await asyncio.gather(*tasks[:4])
    assert queue.status()["m"]["running"] == 0 and most == 3


async def test_the_contextvar_sets_the_priority_of_requests_made_inside():
    queue = ModelQueue(lambda model: (model, 1))
    seen: list[int] = []
    blocker = asyncio.Event()

    async def hold() -> None:
        async with queue.slot("m"):
            await blocker.wait()

    async def ask(level: int) -> None:
        with priority(level):
            async with queue.slot("m"):
                seen.append(level)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    first = asyncio.create_task(ask(BACKGROUND))
    await asyncio.sleep(0)
    second = asyncio.create_task(ask(CHAT))
    await asyncio.sleep(0)
    blocker.set()
    await asyncio.gather(holder, first, second)
    assert seen == [CHAT, BACKGROUND]


async def test_the_transport_holds_a_slot_until_the_answer_is_read():
    queue = ModelQueue(lambda model: ("base", 1))
    inside = 0
    most = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inside, most
        if request.method != "POST":
            return httpx.Response(200, json={"models": []})
        inside += 1
        most = max(most, inside)
        await asyncio.sleep(0.01)
        inside -= 1
        body = json.loads(request.content) if request.content else {}
        return httpx.Response(200, json={"model": body.get("model")})

    client = httpx.AsyncClient(
        transport=QueuedTransport(httpx.MockTransport(handler), queue), base_url="http://o"
    )
    answers = await asyncio.gather(
        client.post("/api/chat", json={"model": "bob-main-x"}),
        client.post("/v1/chat/completions", json={"model": "bob-coder-x"}),
        client.get("/api/tags"),  # not a model run: never queued
    )
    assert [a.status_code for a in answers] == [200, 200, 200]
    assert most == 1 and queue.status()["base"]["served"] == 2
    assert queue.status()["base"]["running"] == 0
    await client.aclose()


def test_roles_on_one_model_share_a_queue(bob):
    # main and coder are both "main-model" in the test config.
    assert bob.models.queue_for("main-model") == ("main-model", 2)
    main = bob.models.spec("main")
    assert bob.models.queue_for(bob.models.alias_name("coder", main)) == ("main-model", 2)
    bob.models.set_limits({"coder": 3})
    assert bob.models.queue_for("main-model") == ("main-model", 3)
    assert bob.models.queue_for("somebody-else") == ("somebody-else", 2)
    with pytest.raises(ValueError, match="from 1 to 8"):
        bob.models.set_limits({"main": 9})
    with pytest.raises(ValueError, match="unknown"):
        bob.models.set_limits({"painter": 1})


# -- several chats over one socket -------------------------------------------------------------


class GatedModel:
    """Answers at once, except a question containing "slow", which waits for ``gate``."""

    def __init__(self) -> None:
        self.gate = threading.Event()

    @staticmethod
    def _question(messages) -> str:
        return " ".join(
            str(part.content)
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )

    async def stream(self, messages, info):
        question = self._question(messages)
        if "slow" in question:
            await asyncio.to_thread(self.gate.wait, 10)
        yield f"answer to {question.split()[-1]}"

    def respond(self, messages, info):  # pragma: no cover - the agent streams
        raise AssertionError("streamed")

    async def factory(self, role: str):
        return FunctionModel(self.respond, stream_function=self.stream)


@pytest.fixture
def chat_client(config, fake_ollama):
    from bob_sdk.ollama import OllamaClient
    from bobd.app import Bob

    fake_ollama.route = {"domain": "general", "needs_tools": False, "risk": "low",
                         "memory_fact": None}  # fmt: skip
    model = GatedModel()
    bob = Bob(
        config,
        ollama=OllamaClient(transport=httpx.MockTransport(fake_ollama.handler)),
        model_factory=model.factory,
        persistent_scheduler=False,
    )
    bob.config.agent.cache.enabled = False
    api = create_api(bob)

    async def startup() -> None:
        await bob.start()

    async def shutdown() -> None:
        model.gate.set()
        await bob.stop()

    api.router.on_startup.append(startup)
    api.router.on_shutdown.append(shutdown)
    with TestClient(api) as client:
        yield client, model, bob


def finals(socket, count: int) -> list[dict]:
    """Read until ``count`` chat answers (or errors) arrived; return every chat message."""
    seen: list[dict] = []
    done = 0
    while done < count:
        event = socket.receive_json()
        if event["type"] == "event":
            continue
        seen.append(event)
        if event["type"] in ("final", "error", "cancelled"):
            done += 1
    return seen


def test_two_chats_run_at_once_and_one_chat_keeps_its_order(chat_client):
    client, model, _bob = chat_client
    with client.websocket_connect("/v1/chat", headers=AUTH) as socket:
        socket.send_json({"type": "ask", "text": "please be slow about alpha", "request_id": "a"})
        socket.send_json({"type": "ask", "text": "quick question about beta", "request_id": "b"})
        first = finals(socket, 1)
        # The quick chat answered while the slow one was still working.
        assert first[-1]["type"] == "final" and first[-1]["request_id"] == "b"
        assert first[-1]["text"] == "answer to beta"
        slow_chat = next(e["conversation_id"] for e in first if e["request_id"] == "a")
        assert slow_chat != first[-1]["conversation_id"]

        # A second message in the slow chat waits for the first.
        socket.send_json({"type": "ask", "text": "and slow gamma", "request_id": "c",
                          "conversation_id": slow_chat})  # fmt: skip
        queued = socket.receive_json()
        while queued["type"] == "event":
            queued = socket.receive_json()
        assert (queued["type"], queued["request_id"]) == ("queued", "c")
        model.gate.set()
        rest = finals(socket, 2)
        answers = [(e["request_id"], e["text"]) for e in rest if e["type"] == "final"]
        assert answers == [("a", "answer to alpha"), ("c", "answer to gamma")]
        assert all(e["conversation_id"] == slow_chat for e in rest)


def test_stop_works_per_chat(chat_client):
    client, model, _bob = chat_client
    with client.websocket_connect("/v1/chat", headers=AUTH) as socket:
        socket.send_json({"type": "ask", "text": "be slow with delta", "request_id": "d"})
        started = socket.receive_json()
        while started["type"] != "started":
            started = socket.receive_json()
        socket.send_json({"type": "ask", "text": "quick one: epsilon", "request_id": "e"})
        socket.send_json({"type": "cancel", "conversation_id": started["conversation_id"]})
        seen = finals(socket, 2)
        outcome = {e["request_id"]: e["type"] for e in seen if e["type"] in ("final", "cancelled")}
        assert outcome == {"d": "cancelled", "e": "final"}
        model.gate.set()


def test_chat_history_lists_names_pins_searches_and_deletes(chat_client):
    client, model, bob = chat_client
    model.gate.set()
    with client.websocket_connect("/v1/chat", headers=AUTH) as socket:
        socket.send_json(
            {"type": "ask", "text": "Plan the Acme onboarding form", "request_id": "1"}
        )
        socket.send_json({"type": "ask", "text": "What is on my calendar", "request_id": "2"})
        seen = finals(socket, 2)
    ids = {e["request_id"]: e["conversation_id"] for e in seen if e["type"] == "final"}

    listing = client.get("/v1/conversations", headers=AUTH).json()["conversations"]
    assert {c["id"] for c in listing} == set(ids.values())
    assert all(c["messages"] == 2 and not c["running"] for c in listing)
    # The model names each chat after its first answer ("a plain answer" from the fake).
    titles = {c["title"] for c in listing}
    assert titles <= {"a plain answer", "Plan the Acme onboarding form", "What is on my calendar"}

    acme = ids["1"]
    renamed = client.patch(f"/v1/conversations/{acme}", headers=AUTH,
                           json={"title": "Acme form", "pinned": True})  # fmt: skip
    assert renamed.json()["title"] == "Acme form" and renamed.json()["pinned"] is True
    asyncio.run(bob.name_chat(acme))  # the model never renames a chat you named
    listing = client.get("/v1/conversations", headers=AUTH).json()["conversations"]
    assert listing[0]["id"] == acme and listing[0]["title"] == "Acme form"

    found = client.get("/v1/conversations", params={"q": "onboarding"}, headers=AUTH).json()
    assert [c["id"] for c in found["conversations"]] == [acme]
    assert "onboarding" in found["conversations"][0]["match"]

    chat = client.get(f"/v1/conversations/{acme}", headers=AUTH).json()
    assert [t["role"] for t in chat["turns"]] == ["user", "assistant"]
    assert chat["turns"][1]["content"] == "answer to form"

    assert client.delete(f"/v1/conversations/{acme}", headers=AUTH).json()["deleted"] is True
    assert client.get(f"/v1/conversations/{acme}", headers=AUTH).status_code == 404
    assert bob.memory.recent_turns(acme) == []  # its messages went with it
    assert client.delete(f"/v1/conversations/{acme}", headers=AUTH).status_code == 404


def test_routine_conversations_stay_out_of_the_chat_list(chat_client):
    client, _model, bob = chat_client
    routine = bob.memory.ensure_conversation("routine:mail-triage:2026-11-16")
    bob.memory.add_turn(routine, "user", "Write my morning brief")
    listing = client.get("/v1/conversations", headers=AUTH).json()["conversations"]
    assert routine not in {c["id"] for c in listing}


def test_model_limits_can_be_read_and_changed(chat_client):
    client, _model, _bob = chat_client
    queue = client.get("/v1/models/queue", headers=AUTH).json()
    assert queue["limits"]["main"] == 2 and queue["limits"]["router"] == 4
    changed = client.put("/v1/models/limits", headers=AUTH, json={"limits": {"main": 3}})
    assert changed.json()["limits"]["main"] == 3
    bad = client.put("/v1/models/limits", headers=AUTH, json={"limits": {"main": 0}})
    assert bad.status_code == 400
    roles = client.get("/v1/models", headers=AUTH).json()["roles"]
    assert roles["main"]["parallel"] == 3
