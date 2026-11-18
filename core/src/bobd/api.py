"""The app's API: FastAPI on 127.0.0.1 with a WebSocket for streaming.

Every route needs the bearer token from the Keychain, except ``/health``.
Routes under ``/internal`` are for plugin servers and need that plugin's own
token, so the core knows which manifest scopes the call.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
from pathlib import Path
from typing import Any, Literal

from bob_sdk.ollama import OllamaError
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import __version__, evals
from .app import KNOWLEDGE, Bob
from .auth import delete_secret, get_secret, set_secret, tokens_match
from .broker import Caller, ToolDenied, ToolFailed
from .modelqueue import BACKGROUND, CHAT, priority
from .plugins import ConsentRequired, Plugin, PluginError
from .pluginwheel import (
    MAX_WHEEL_BYTES,
    WheelError,
    inspect_wheel,
    install_wheel,
    installed,
    remove_plugin,
)

log = logging.getLogger(__name__)

GITHUB_SECRET = "github-token"


class AskBody(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    conversation_id: str | None = None
    specialist: str | None = None
    # Skip the answer cache and work the answer out again.
    fresh: bool = False


class WheelBody(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    data_b64: str = Field(min_length=1)


class WheelInstallBody(BaseModel):
    sha256: str = Field(min_length=64, max_length=64)


class ConfirmBody(BaseModel):
    approved: bool


class EnableBody(BaseModel):
    acknowledged: bool = False


class SettingsBody(BaseModel):
    settings: dict[str, Any]


class ToggleBody(BaseModel):
    enabled: bool


class ToolCallBody(BaseModel):
    plugin: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelBody(BaseModel):
    model: str


class LimitsBody(BaseModel):
    limits: dict[str, int]


class ConversationBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None


class RoutineBody(BaseModel):
    enabled: bool | None = None
    cron: str | None = None
    every_minutes: int | None = None
    between: tuple[str, str] | None = None


class FactBody(BaseModel):
    fact: str = Field(min_length=1, max_length=1000)


class ReindexBody(BaseModel):
    collection: str | None = None
    source: str | None = None
    force: bool = False


class FileBody(BaseModel):
    collection: str
    path: str


class GitHubAuthBody(BaseModel):
    auth: Literal["gh", "token"]
    token: str | None = None


class SummarizeBody(BaseModel):
    text: str
    kind: str = "email"


class GenerateBody(BaseModel):
    role: Literal["router", "main", "coder", "vision"]
    system: str
    prompt: str


class AuditBody(BaseModel):
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    outcome: str


class PublishBody(BaseModel):
    topic: str
    data: dict[str, Any] = Field(default_factory=dict)


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _bundled_ids(bob: Bob) -> set[str]:
    """Plugin ids that come with Bob, which a wheel may not replace."""
    folder = bob.config.user_site_dir
    added = {p["plugin"] for p in installed(folder)} if folder is not None else set()
    return set(bob.plugins.plugins) - added


def create_api(bob: Bob) -> FastAPI:
    api = FastAPI(title="bobd", version=__version__, docs_url=None, redoc_url=None)

    def require_user(request: Request) -> None:
        given = _bearer(request.headers.get("authorization"))
        if given is None or not tokens_match(given, bob.token):
            raise HTTPException(401, "missing or wrong bearer token")

    def require_plugin(request: Request) -> Plugin:
        given = _bearer(request.headers.get("authorization"))
        plugin = bob.plugins.by_token(given) if given else None
        if plugin is None:
            raise HTTPException(401, "unknown plugin token")
        return plugin

    user = [Depends(require_user)]

    def not_found(exc: Exception) -> HTTPException:
        return HTTPException(404, str(exc).strip("'\""))

    async def tool(plugin: str, name: str, arguments: dict[str, Any], caller: Caller) -> Any:
        try:
            return await bob.broker.call(plugin, name, arguments, caller)
        except ToolDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ToolFailed as exc:
            raise HTTPException(502, str(exc)) from exc

    app_user = Caller(kind="user", name="user", interactive=True)

    # -- health and status --------------------------------------------------------

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": bob.started, "version": __version__}

    @api.get("/metrics")
    async def metrics() -> Response:
        return Response(bob.telemetry.metrics_text(), media_type="text/plain; version=0.0.4")

    @api.get("/v1/status", dependencies=user)
    async def status() -> dict[str, Any]:
        return {
            "version": __version__,
            "models": await bob.models.health(),
            "plugins": {p.id: p.status for p in bob.plugins.plugins.values()},
            "specialists": [s.id for s in bob.orchestrator.specialists()],
            "pending_confirmations": len(bob.confirmations.pending()),
            "conformance": None if bob.last_conformance is None else bob.last_conformance.as_dict(),
        }

    # -- asking ----------------------------------------------------------------------

    @api.post("/v1/ask", dependencies=user)
    async def ask(body: AskBody) -> dict[str, Any]:
        try:
            with priority(CHAT):  # `bob ask` waits for the answer
                reply = await bob.orchestrator.handle(
                    body.text,
                    conversation_id=bob.memory.start_chat(body.conversation_id, body.text),
                    force_specialist=body.specialist,
                    fresh=body.fresh,
                )
        except (RuntimeError, OllamaError) as exc:
            raise HTTPException(503, str(exc)) from exc
        return reply.as_dict()

    @api.get("/v1/cache", dependencies=user)
    async def cache_stats() -> dict[str, Any]:
        return bob.answer_cache.stats()

    @api.delete("/v1/cache", dependencies=user)
    async def cache_clear() -> dict[str, Any]:
        return {"removed": bob.answer_cache.clear()}

    # Chats being answered right now, across every socket: conversation -> its tasks.
    # Different chats run at the same time; within one chat, messages are answered in
    # order (the per-chat lock), and the model scheduler decides who waits for a model.
    running: dict[str, set[asyncio.Task[None]]] = {}
    chat_locks: dict[str, asyncio.Lock] = {}

    def cancel_chat(conversation: str) -> int:
        tasks = [task for task in running.get(conversation, set()) if not task.done()]
        for task in tasks:
            task.cancel()
        return len(tasks)

    @api.websocket("/v1/chat")
    async def chat(socket: WebSocket) -> None:
        given = _bearer(socket.headers.get("authorization"))
        if given is None or not tokens_match(given, bob.token):
            await socket.close(code=4401)
            return
        await socket.accept()
        send_lock = asyncio.Lock()
        open_ = True

        async def emit(event: dict[str, Any]) -> None:
            # Once the app has gone, answers still finish and are saved to the chat.
            nonlocal open_
            if not open_:
                return
            async with send_lock:
                try:
                    await socket.send_json(event)
                except Exception:  # noqa: BLE001 - the socket closed under us
                    open_ = False

        sessions: set[str] = set()
        mine: set[asyncio.Task[None]] = set()

        async def forward_events() -> None:
            # One socket carries everything the app shows: chat, confirm cards for
            # this socket's conversations, and every bus event for cards and badges.
            async with bob.events.subscribe() as queue:
                while True:
                    event = await queue.get()
                    if event.topic == "confirm.requested":
                        session = event.data.get("session")
                        if session is None or session in sessions:
                            await emit({"type": "confirm_request", **event.data})
                        continue
                    await emit({"type": "event", **event.as_dict()})

        async def answer(
            text: str,
            conversation: str,
            specialist: str | None,
            fresh: bool,
            request: str,
            voice: bool = False,
        ) -> None:
            async def tagged(event: dict[str, Any]) -> None:
                await emit({**event, "conversation_id": conversation, "request_id": request})

            lock = chat_locks.setdefault(conversation, asyncio.Lock())
            try:
                if lock.locked():
                    await tagged({"type": "queued"})
                async with lock:
                    await tagged({"type": "started"})
                    with priority(CHAT):  # you are waiting for this one
                        await bob.orchestrator.handle(
                            text, conversation_id=conversation, interactive=True,
                            emit=tagged, force_specialist=specialist, fresh=fresh, voice=voice,
                        )  # fmt: skip
                if bob.memory.needs_title(conversation):
                    asyncio.create_task(bob.name_chat(conversation))  # noqa: RUF006
            except asyncio.CancelledError:
                await tagged({"type": "cancelled"})
                raise
            except Exception as exc:  # report to the chat instead of dropping the socket
                await tagged({"type": "error", "message": str(exc)})

        def start(task: asyncio.Task[None], conversation: str) -> None:
            running.setdefault(conversation, set()).add(task)
            mine.add(task)

            def done(finished: asyncio.Task[None]) -> None:
                mine.discard(finished)
                tasks = running.get(conversation)
                if tasks is not None:
                    tasks.discard(finished)
                    if not tasks:
                        running.pop(conversation, None)
                        lock = chat_locks.get(conversation)
                        if lock is not None and not lock.locked():
                            chat_locks.pop(conversation, None)

            task.add_done_callback(done)

        forwarder = asyncio.create_task(forward_events())
        try:
            while True:
                message = await socket.receive_json()
                kind = message.get("type")
                if kind == "ask":
                    text = str(message.get("text", "")).strip()
                    request = str(message.get("request_id") or "")[:64]
                    if not text:
                        await emit({"type": "error", "message": "the request is empty",
                                    "request_id": request})  # fmt: skip
                        continue
                    conversation = bob.memory.start_chat(message.get("conversation_id"), text)
                    sessions.add(conversation)
                    task = asyncio.create_task(
                        answer(
                            text,
                            conversation,
                            message.get("specialist"),
                            message.get("fresh") is True,
                            request,
                            message.get("input") == "voice",
                        )  # fmt: skip
                    )
                    start(task, conversation)
                elif kind == "confirm":
                    resolved = bob.confirmations.resolve(
                        str(message.get("id", "")), bool(message.get("approved"))
                    )
                    await emit({"type": "confirm_ack", "id": message.get("id"), "ok": resolved})
                elif kind == "cancel":
                    # Stop one chat; with no chat named, everything this window started.
                    conversation = message.get("conversation_id")
                    if conversation:
                        cancel_chat(str(conversation))
                    else:
                        for task in list(mine):
                            task.cancel()
                elif kind == "watch":
                    # The app opened a chat started elsewhere: show its confirm cards here.
                    conversation = str(message.get("conversation_id") or "")
                    if conversation:
                        sessions.add(conversation)
                else:
                    await emit({"type": "error", "message": f"unknown message type {kind!r}"})
        except WebSocketDisconnect:
            pass
        finally:
            open_ = False
            forwarder.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await forwarder

    # -- chat history ----------------------------------------------------------------------

    @api.get("/v1/conversations", dependencies=user)
    async def conversations(q: str = "", limit: int = 200) -> dict[str, Any]:
        found = bob.memory.conversations(q, limit)
        for entry in found:
            entry["running"] = entry["id"] in running
        return {"conversations": found}

    @api.get("/v1/conversations/{conversation_id}", dependencies=user)
    async def conversation(conversation_id: str) -> dict[str, Any]:
        found = bob.memory.conversation(conversation_id)
        if found is None:
            raise HTTPException(404, "there is no such chat")
        return {**found, "running": conversation_id in running}

    @api.patch("/v1/conversations/{conversation_id}", dependencies=user)
    async def update_conversation(conversation_id: str, body: ConversationBody) -> dict[str, Any]:
        if bob.memory.conversation(conversation_id) is None:
            raise HTTPException(404, "there is no such chat")
        if body.title is not None and not bob.memory.set_title(
            conversation_id, body.title, by_you=True
        ):
            raise HTTPException(400, "a title needs some text")
        if body.pinned is not None:
            bob.memory.set_pinned(conversation_id, body.pinned)
        updated = bob.memory.conversation(conversation_id) or {}
        await bob.events.publish("chat.updated", id=conversation_id, title=updated.get("title"))
        return {key: value for key, value in updated.items() if key != "turns"}

    @api.delete("/v1/conversations/{conversation_id}", dependencies=user)
    async def delete_conversation(conversation_id: str) -> dict[str, Any]:
        stopped = cancel_chat(conversation_id)
        if stopped:
            await asyncio.sleep(0)  # let the cancelled answer finish before deleting
        if not bob.memory.delete_conversation(conversation_id):
            raise HTTPException(404, "there is no such chat")
        bob.grants.revoke_session(conversation_id)
        await bob.events.publish("chat.deleted", id=conversation_id)
        return {"deleted": True, "stopped": stopped}

    # -- folders Bob may work in, per chat --------------------------------------------------

    @api.get("/v1/grants", dependencies=user)
    async def list_grants() -> dict[str, Any]:
        return {"grants": [grant.as_dict() for grant in bob.grants.list()]}

    @api.delete("/v1/grants/{grant_id}", dependencies=user)
    async def revoke_grant(grant_id: str) -> dict[str, Any]:
        if not bob.grants.revoke(grant_id):
            raise HTTPException(404, "there is no such folder grant")
        bob.audit.record(actor="user", kind="grant", action=f"revoke {grant_id}", outcome="ok")
        return {"revoked": True}

    @api.websocket("/v1/events")
    async def events(socket: WebSocket) -> None:
        given = _bearer(socket.headers.get("authorization"))
        if given is None or not tokens_match(given, bob.token):
            await socket.close(code=4401)
            return
        await socket.accept()
        try:
            async with bob.events.subscribe() as queue:
                while True:
                    await socket.send_json((await queue.get()).as_dict())
        except (WebSocketDisconnect, RuntimeError):
            pass

    # -- confirm cards ------------------------------------------------------------------

    @api.get("/v1/confirmations", dependencies=user)
    async def confirmations() -> list[dict[str, Any]]:
        return [item.as_dict() for item in bob.confirmations.pending()]

    @api.post("/v1/confirmations/{confirmation_id}", dependencies=user)
    async def confirm(confirmation_id: str, body: ConfirmBody) -> dict[str, Any]:
        if not bob.confirmations.resolve(confirmation_id, body.approved):
            raise HTTPException(404, "that confirm card has expired or was already answered")
        return {"ok": True}

    # -- plugins -----------------------------------------------------------------------

    @api.get("/v1/plugins", dependencies=user)
    async def plugins() -> dict[str, Any]:
        return {
            "plugins": [
                bob.plugins.summary(plugin_id) for plugin_id in sorted(bob.plugins.plugins)
            ],
            "rejected": bob.plugins.rejected,
        }

    @api.post("/v1/plugins/rescan", dependencies=user)
    async def rescan() -> dict[str, Any]:
        bob.plugins.discover()
        return await plugins()

    # Adding a plugin from a wheel file: inspect first, install only what was inspected.

    def wheel_folder() -> Path:
        folder = bob.config.user_site_dir
        if folder is None:
            raise HTTPException(400, "adding plugins from files is not set up (paths.user_site)")
        return folder

    def wheel_inbox() -> Path:
        inbox = bob.config.data_dir / "wheel-inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        return inbox

    @api.post("/v1/plugins/wheel/inspect", dependencies=user)
    async def wheel_inspect(body: WheelBody) -> dict[str, Any]:
        wheel_folder()
        name = Path(body.filename).name
        try:
            data = base64.b64decode(body.data_b64, validate=True)
        except ValueError as exc:
            raise HTTPException(400, "the file did not arrive intact") from exc
        if len(data) > MAX_WHEEL_BYTES:
            raise HTTPException(413, "the file is too large for a plugin")
        inbox = wheel_inbox()
        staged = inbox / f"{hashlib.sha256(data).hexdigest()}.whl"
        staged.write_bytes(data)
        try:
            found = inspect_wheel(staged, taken=_bundled_ids(bob))
        except WheelError as exc:
            staged.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from exc
        return {**found.as_dict(), "filename": name}

    @api.post("/v1/plugins/wheel/install", dependencies=user)
    async def wheel_install(body: WheelInstallBody) -> dict[str, Any]:
        folder = wheel_folder()
        staged = wheel_inbox() / f"{body.sha256}.whl"
        if not staged.is_file():
            raise HTTPException(404, "that file is no longer here; add it again")
        try:
            done = install_wheel(staged, folder, sha256=body.sha256, taken=_bundled_ids(bob))
        except WheelError as exc:
            raise HTTPException(400, str(exc)) from exc
        finally:
            staged.unlink(missing_ok=True)
        bob.audit.record(
            actor="user", kind="plugin", action=f"add {done.plugin_id} {done.version} from a wheel",
            outcome="ok", detail=done.sha256,
        )  # fmt: skip
        bob.plugins.discover()
        await bob.plugins.reload(done.plugin_id)  # an update replaces code a plugin is running
        return {"installed": done.as_dict(), "plugins": await plugins()}

    @api.get("/v1/plugins/wheel/installed", dependencies=user)
    async def wheel_installed() -> dict[str, Any]:
        return {"plugins": installed(wheel_folder())}

    @api.delete("/v1/plugins/wheel/{dist}", dependencies=user)
    async def wheel_remove(dist: str) -> dict[str, Any]:
        folder = wheel_folder()
        record = next((p for p in installed(folder) if p["dist"] == dist), None)
        if record is not None and record["plugin"] in bob.plugins.plugins:
            with contextlib.suppress(PluginError):
                await bob.plugins.disable(record["plugin"])
        try:
            remove_plugin(dist, folder)
        except WheelError as exc:
            raise HTTPException(404, str(exc)) from exc
        bob.audit.record(
            actor="user", kind="plugin", action=f"remove {dist}", outcome="ok", detail=""
        )  # fmt: skip
        return {"ok": True, "restart_needed": True}

    @api.get("/v1/plugins/{plugin_id}", dependencies=user)
    async def plugin(plugin_id: str) -> dict[str, Any]:
        try:
            return bob.plugins.summary(plugin_id)
        except PluginError as exc:
            raise not_found(exc) from exc

    @api.post("/v1/plugins/{plugin_id}/enable", dependencies=user)
    async def enable(plugin_id: str, body: EnableBody) -> dict[str, Any]:
        try:
            return await bob.plugins.enable(plugin_id, acknowledged=body.acknowledged)
        except ConsentRequired as exc:
            # The app shows this summary (tools, tiers, folders, network) and asks.
            raise HTTPException(409, {"consent_required": True, "plugin": exc.summary}) from exc
        except PluginError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/v1/plugins/{plugin_id}/disable", dependencies=user)
    async def disable(plugin_id: str) -> dict[str, Any]:
        try:
            return await bob.plugins.disable(plugin_id)
        except PluginError as exc:
            raise not_found(exc) from exc

    @api.put("/v1/plugins/{plugin_id}/settings", dependencies=user)
    async def plugin_settings(plugin_id: str, body: SettingsBody) -> dict[str, Any]:
        try:
            return await bob.plugins.update_settings(plugin_id, body.settings)
        except PluginError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.put("/v1/plugins/{plugin_id}/tools/{tool_name}", dependencies=user)
    async def plugin_tool(plugin_id: str, tool_name: str, body: ToggleBody) -> dict[str, Any]:
        try:
            await bob.plugins.set_tool_enabled(plugin_id, tool_name, body.enabled)
        except PluginError as exc:
            raise HTTPException(400, str(exc)) from exc
        return bob.plugins.summary(plugin_id)

    @api.post("/v1/tools/call", dependencies=user)
    async def call_tool(body: ToolCallBody) -> dict[str, Any]:
        return {"result": await tool(body.plugin, body.tool, body.arguments, app_user)}

    # -- knowledge ------------------------------------------------------------------------

    @api.get("/v1/knowledge/collections", dependencies=user)
    async def knowledge_collections() -> dict[str, Any]:
        configured = [c.model_dump(exclude_none=True) for c in bob.collections()]
        statuses: dict[str, Any] = {}
        plugin_ = bob.plugins.plugins.get(KNOWLEDGE)
        if plugin_ is not None and plugin_.running:
            listing = await tool(KNOWLEDGE, "list_collections", {}, app_user)
            for item in listing.get("collections", []):
                statuses[item["id"]] = await tool(
                    KNOWLEDGE, "status", {"collection": item["id"]}, app_user
                )
        return {"collections": configured, "status": statuses}

    @api.put("/v1/knowledge/collections", dependencies=user)
    async def upsert_collection(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return (await bob.upsert_collection(body)).model_dump(exclude_none=True)
        except (ValueError, PluginError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.delete("/v1/knowledge/collections/{collection_id}", dependencies=user)
    async def delete_collection(collection_id: str) -> dict[str, Any]:
        try:
            await bob.remove_collection(collection_id)
        except KeyError as exc:
            raise not_found(exc) from exc
        return {"ok": True}

    @api.post("/v1/knowledge/reindex", dependencies=user)
    async def reindex(body: ReindexBody) -> Any:
        arguments = {k: v for k, v in body.model_dump().items() if v not in (None, False)}
        return await tool(KNOWLEDGE, "reindex", arguments, app_user)

    @api.post("/v1/knowledge/files", dependencies=user)
    async def add_file(body: FileBody) -> Any:
        return await tool(KNOWLEDGE, "add_file", body.model_dump(), app_user)

    @api.get("/v1/knowledge/github", dependencies=user)
    async def github_auth() -> dict[str, Any]:
        return {
            "auth": bob.knowledge_extra()["github_auth"],
            "token_stored": get_secret(GITHUB_SECRET) is not None,
        }

    @api.put("/v1/knowledge/github", dependencies=user)
    async def set_github_auth(body: GitHubAuthBody) -> dict[str, Any]:
        """Choose the GitHub auth mode. The token goes to the Keychain, never to disk."""
        if body.token is not None:
            if not body.token.strip():
                delete_secret(GITHUB_SECRET)
            elif not set_secret(GITHUB_SECRET, body.token.strip()):
                raise HTTPException(500, "the Keychain is not available to store the token")
        bob.db.set_setting(KNOWLEDGE, "github_auth", body.auth)
        bob.audit.record(
            actor="user", kind="settings", action="knowledge.github_auth", outcome="ok",
            arguments={"auth": body.auth, "token": "***" if body.token else None},
        )  # fmt: skip
        plugin_ = bob.plugins.plugins.get(KNOWLEDGE)
        if plugin_ is not None and plugin_.running:
            await bob.plugins.restart(KNOWLEDGE)
        return await github_auth()

    # -- models ------------------------------------------------------------------------------

    @api.get("/v1/models", dependencies=user)
    async def models() -> dict[str, Any]:
        return await bob.models.health()

    @api.get("/v1/models/queue", dependencies=user)
    async def model_queue() -> dict[str, Any]:
        return {"limits": bob.models.limits(), "queues": bob.model_queue.status()}

    @api.put("/v1/models/limits", dependencies=user)
    async def set_model_limits(body: LimitsBody) -> dict[str, Any]:
        try:
            limits = bob.models.set_limits(body.limits)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        bob.model_queue.wake_all()
        bob.audit.record(
            actor="user", kind="settings", action="models.limits", outcome="ok", arguments=limits
        )
        return {"limits": limits}

    @api.put("/v1/models/{role}", dependencies=user)
    async def set_model(role: str, body: ModelBody) -> dict[str, Any]:
        try:
            spec = await bob.models.set_role(role, body.model)
        except (ValueError, OllamaError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return spec.model_dump()

    # -- routines ------------------------------------------------------------------------------

    def routine_view(routine) -> dict[str, Any]:
        return {**routine.as_dict(), "next_run": bob.scheduler.next_run(routine.id)}

    @api.get("/v1/routines", dependencies=user)
    async def routines() -> list[dict[str, Any]]:
        return [routine_view(routine) for routine in bob.scheduler.routines()]

    @api.put("/v1/routines/{routine_id}", dependencies=user)
    async def configure_routine(routine_id: str, body: RoutineBody) -> dict[str, Any]:
        changes = body.model_dump(exclude_none=True)
        if "between" in changes:
            changes["between"] = list(changes["between"])
        try:
            return routine_view(bob.scheduler.configure(routine_id, changes))
        except KeyError as exc:
            raise not_found(exc) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/v1/routines/{routine_id}/run", dependencies=user)
    async def run_routine(routine_id: str) -> dict[str, Any]:
        try:
            bob.scheduler.get(routine_id)
        except KeyError as exc:
            raise not_found(exc) from exc
        return await bob.scheduler.run(routine_id, trigger="manual")

    @api.get("/v1/routines/{routine_id}/runs", dependencies=user)
    async def routine_runs(routine_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return bob.scheduler.history(routine_id, limit=max(1, min(limit, 200)))

    # -- memory, cards, audit, widget ------------------------------------------------------------

    @api.get("/v1/memory/facts", dependencies=user)
    async def facts() -> list[dict[str, Any]]:
        return [vars(fact) for fact in bob.memory.facts()]

    @api.post("/v1/memory/facts", dependencies=user)
    async def add_fact(body: FactBody) -> dict[str, Any]:
        return vars(bob.memory.add_fact(body.fact, source="user"))

    @api.delete("/v1/memory/facts/{fact_id}", dependencies=user)
    async def delete_fact(fact_id: int) -> dict[str, Any]:
        if not bob.memory.delete_fact(fact_id):
            raise HTTPException(404, "no such fact")
        return {"ok": True}

    @api.get("/v1/cards", dependencies=user)
    async def cards(limit: int = 20) -> list[dict[str, Any]]:
        return bob.cards(limit=max(1, min(limit, 200)))

    @api.get("/v1/audit", dependencies=user)
    async def audit(limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
        return [vars(entry) for entry in bob.audit.entries(limit=limit, kind=kind)]

    @api.get("/v1/widget", dependencies=user)
    async def widget() -> dict[str, Any]:
        return await bob.widget_snapshot()

    @api.post("/v1/conformance/run", dependencies=user)
    async def conformance() -> dict[str, Any]:
        return (await bob.run_conformance()).as_dict()

    @api.get("/v1/evals", dependencies=user)
    async def list_evals() -> dict[str, Any]:
        return {"available": evals.available(bob), "history": evals.history(bob)}

    @api.post("/v1/evals/{eval_id}/run", dependencies=user)
    async def run_eval(eval_id: str) -> dict[str, Any]:
        try:
            return (await evals.run_eval(bob, eval_id)).as_dict()
        except evals.EvalError as exc:
            raise HTTPException(400, str(exc)) from exc

    # -- for plugin servers ------------------------------------------------------------------------

    @api.post("/internal/call")
    async def internal_call(
        body: ToolCallBody,
        plugin_: Plugin = Depends(require_plugin),  # noqa: B008
    ) -> dict[str, Any]:
        caller = Caller(kind="plugin", name=plugin_.id, plugin=plugin_.id)
        return {"result": await tool(body.plugin, body.tool, body.arguments, caller)}

    @api.post("/internal/summarize")
    async def internal_summarize(
        body: SummarizeBody,
        _plugin: Plugin = Depends(require_plugin),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            with priority(CHAT if bob.broker.serving_chat(_plugin.id) else BACKGROUND):
                return (await bob.quarantine.summarise(body.text, kind=body.kind)).model_dump()
        except OllamaError as exc:
            raise HTTPException(503, str(exc)) from exc

    @api.post("/internal/generate")
    async def internal_generate(
        body: GenerateBody,
        _plugin: Plugin = Depends(require_plugin),  # noqa: B008
    ) -> dict[str, Any]:
        try:
            # A plugin working for a chat (mail summaries while you wait) goes first; a
            # plugin working on its own (a form build) waits for chats.
            with priority(CHAT if bob.broker.serving_chat(_plugin.id) else BACKGROUND):
                return {"text": await bob.generate(body.role, body.system, body.prompt)}
        except OllamaError as exc:
            raise HTTPException(503, str(exc)) from exc

    @api.post("/internal/audit")
    async def internal_audit(
        body: AuditBody,
        plugin_: Plugin = Depends(require_plugin),  # noqa: B008
    ) -> dict[str, Any]:
        kind = "git" if body.action.startswith("git ") else "plugin"
        bob.audit.record(
            actor=f"plugin:{plugin_.id}", kind=kind, action=body.action,
            outcome=body.outcome, arguments=body.arguments,
        )  # fmt: skip
        return {"ok": True}

    @api.post("/internal/publish")
    async def internal_publish(
        body: PublishBody,
        plugin_: Plugin = Depends(require_plugin),  # noqa: B008
    ) -> dict[str, Any]:
        # A plugin may only publish under its own name.
        data = {key: value for key, value in body.data.items() if key != "topic"}
        await bob.events.publish(f"{plugin_.id}.{body.topic.lstrip('.')}", **data)
        return {"ok": True}

    return api
