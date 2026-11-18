"""The tool broker: the one path by which any tool is called.

Models, routines, the app and other plugins all call tools through here, so
the permission rules are enforced in exactly one place:

* the plugin is enabled and running, and the tool is declared in its manifest;
* a tool you switched off cannot be called by anyone;
* admin tools are never available to a model or to another plugin;
* a knowledge search is limited to the collections in the caller's ``uses``;
* an ``external``-tier call waits for your approval, and is audited.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from bob_sdk.manifest import Tier
from bob_sdk.writeguard import Refused, check_read, check_write
from mcp.types import CallToolResult

from .audit import AuditLog
from .confirm import ConfirmationHub
from .events import EventBus
from .grants import GrantStore, folder_you_named
from .modelqueue import CHAT, current_priority
from .plugins import PluginError, PluginManager
from .telemetry import Telemetry

log = logging.getLogger(__name__)

KNOWLEDGE_PLUGIN = "knowledge"
ActorKind = Literal["user", "model", "routine", "plugin"]
# Arguments that hold a file or folder path, in tools that read or change your files.
PATH_ARGUMENTS = ("path", "source", "destination")


class ToolDenied(Exception):
    """The call is not allowed. The message is safe to show to a model."""


class ToolFailed(Exception):
    """The tool ran and reported an error."""


@dataclass(frozen=True)
class Caller:
    """Who is asking, and on whose behalf permissions are checked."""

    kind: ActorKind
    name: str  # "user", a routine id, or a plugin id
    # The plugin whose manifest scopes the call (the specialist's plugin for a model).
    plugin: str | None = None
    session: str | None = None  # conversation that can show a confirm card
    interactive: bool = False
    # What the user has written in this conversation: the only place a folder can be named.
    said: str = ""

    @property
    def label(self) -> str:
        return self.name if self.kind == "user" else f"{self.kind}:{self.name}"


USER = Caller(kind="user", name="user", interactive=False)


def parse_result(result: CallToolResult) -> Any:
    """Turn an MCP tool result into plain data."""
    if result.structured_content is not None:
        content = result.structured_content
        if isinstance(content, dict) and set(content) == {"result"}:
            return content["result"]
        return content
    texts = [block.text for block in result.content if getattr(block, "type", "") == "text"]
    joined = "\n".join(texts)
    if len(texts) == 1:
        try:
            return json.loads(joined)
        except ValueError:
            return joined
    parsed: list[Any] = []
    for text in texts:
        try:
            parsed.append(json.loads(text))
        except ValueError:
            return joined
    return parsed


def _error_text(result: CallToolResult) -> str:
    texts = [block.text for block in result.content if getattr(block, "type", "") == "text"]
    return "\n".join(texts) or "the tool reported an error"


class ToolBroker:
    def __init__(
        self,
        plugins: PluginManager,
        confirmations: ConfirmationHub,
        audit: AuditLog,
        telemetry: Telemetry,
        events: EventBus | None = None,
        grants: GrantStore | None = None,
        protected: list[Path] | None = None,
    ) -> None:
        self.grants = grants or GrantStore()
        self._protected = protected or []
        self._events = events
        self._plugins = plugins
        self._confirm = confirmations
        self._audit = audit
        self._telemetry = telemetry
        # Tool calls each plugin is running for a chat right now. While a plugin serves a
        # chat, the model requests it makes (summaries, generate) are a chat's too.
        self._for_chat: Counter[str] = Counter()

    def serving_chat(self, plugin_id: str) -> bool:
        return self._for_chat[plugin_id] > 0

    def check(self, plugin_id: str, tool: str, arguments: dict[str, Any], caller: Caller) -> Tier:
        """Every rule that does not need your decision. Raises :class:`ToolDenied`."""
        try:
            plugin = self._plugins.get(plugin_id)
        except PluginError as exc:
            raise ToolDenied(str(exc)) from exc
        if not plugin.running:
            raise ToolDenied(f"plugin {plugin_id!r} is switched off")
        tier = plugin.manifest.tools.get(tool)
        if tier is None or tool not in plugin.mcp_tools:
            raise ToolDenied(f"plugin {plugin_id!r} has no tool {tool!r}")
        if tool in self._plugins.disabled_tools(plugin_id):
            raise ToolDenied(f"tool {plugin_id}.{tool} is switched off in Settings")
        if caller.kind in ("model", "plugin"):
            if tool in plugin.manifest.admin_tools:
                raise ToolDenied(f"tool {plugin_id}.{tool} can only be run by you")
            self._check_scope(plugin_id, tool, arguments, caller)
        if caller.kind == "routine" and (tier.needs_confirmation or tier.needs_grant):
            raise ToolDenied("a routine cannot run an action that needs your approval")
        return tier

    async def _grant_paths(
        self, plugin_id: str, tool: str, arguments: dict[str, Any], tier: Tier, caller: Caller
    ) -> dict[str, Any]:
        """Tools that read or change your files: every path must be inside a folder you named.

        Returns the arguments with each path made absolute and the chat id filled in by the
        core (a model's own value is dropped). Raises :class:`ToolDenied` otherwise.
        """
        if caller.kind not in ("model", "user"):
            raise ToolDenied(f"{plugin_id}.{tool} can only be run by you or in a chat with you")
        arguments = {k: v for k, v in arguments.items() if k != "session"}
        arguments["session"] = caller.session or ""
        keys = [key for key in PATH_ARGUMENTS if arguments.get(key)]
        for key in keys:
            try:
                target = (
                    check_read(arguments[key])
                    if tier is Tier.READ_GRANTED
                    else check_write(arguments[key], self._protected)
                )
            except Refused as exc:
                raise ToolDenied(str(exc)) from exc
            arguments[key] = str(target)
            if caller.kind == "user":
                continue  # you, from the app: your own click is the approval
            await self._grant_for(target, caller, plugin_id)
        return arguments

    async def _grant_for(self, target: Path, caller: Caller, plugin_id: str) -> None:
        if not caller.session:
            raise ToolDenied("there is no chat to attach a folder grant to")
        if self.grants.covering(caller.session, target) is not None:
            return
        named = folder_you_named(caller.said, target, self._protected)
        if named is None:
            raise ToolDenied(
                f"{target} is not in a folder the user named. Ask the user for the path of the "
                "folder to work in (in quotes if it has spaces) and use exactly that; do not guess a folder"
            )
        root, file_only = named
        if not caller.interactive:
            raise ToolDenied("this needs the user's approval; ask in the panel so a card can show")
        approved = await self._confirm.request(
            plugin=plugin_id,
            tool="allow_folder",
            arguments={
                "folder": str(root),
                "allows": "read and change files "
                + ("this one file" if file_only else "in it and everything inside it"),
                "lasts": "this chat only",
                "changes can be undone": "yes",
            },
            actor=caller.label,
            session=caller.session,
        )
        if not approved:
            self._audit.record(
                actor=caller.label, kind="grant", action=f"{plugin_id}: {root}", outcome="declined"
            )
            raise ToolDenied(f"you declined access to {root}; do not ask again")
        grant = self.grants.add(caller.session, root, file_only=file_only)
        self._audit.record(
            actor=caller.label, kind="grant", action=f"{plugin_id}: {root}", outcome="ok",
            detail=f"chat {caller.session}, grant {grant.id}",
        )  # fmt: skip

    def _check_scope(
        self, plugin_id: str, tool: str, arguments: dict[str, Any], caller: Caller
    ) -> None:
        if caller.plugin is None:
            raise ToolDenied("the caller has no plugin identity")
        try:
            scope = self._plugins.get(caller.plugin).manifest
        except PluginError as exc:
            raise ToolDenied(str(exc)) from exc
        if caller.kind == "plugin" and plugin_id != KNOWLEDGE_PLUGIN:
            # Plugins never talk to each other directly. A call goes through the
            # core and must be declared under `calls` in the caller's manifest.
            if f"{plugin_id}.{tool}" not in scope.calls:
                raise ToolDenied(f"plugin {caller.plugin!r} may not call {plugin_id}.{tool}")
            if self._plugins.tier(plugin_id, tool).needs_confirmation:
                raise ToolDenied("a plugin cannot run an action that needs your approval")
        if plugin_id == KNOWLEDGE_PLUGIN and "collection" in arguments:
            collection = str(arguments["collection"])
            if not scope.knowledge.may_read(collection):
                raise ToolDenied(
                    f"plugin {caller.plugin!r} may not read the collection {collection!r}"
                )

    async def call(
        self, plugin_id: str, tool: str, arguments: dict[str, Any] | None, caller: Caller
    ) -> Any:
        arguments = dict(arguments or {})
        action = f"{plugin_id}.{tool}"
        started = time.perf_counter()
        try:
            tier = self.check(plugin_id, tool, arguments, caller)
        except ToolDenied as exc:
            self._audit.record(
                actor=caller.label, kind="tool", action=action, outcome="denied",
                arguments=arguments, detail=str(exc),
            )  # fmt: skip
            self._telemetry.tool_call(plugin_id, tool, "denied", time.perf_counter() - started)
            raise

        if tier.needs_grant:
            try:
                arguments = await self._grant_paths(plugin_id, tool, arguments, tier, caller)
            except ToolDenied as exc:
                self._audit.record(
                    actor=caller.label, kind="tool", action=action, tier=tier.value,
                    outcome="denied", arguments=arguments, detail=str(exc),
                )  # fmt: skip
                self._telemetry.tool_call(plugin_id, tool, "denied", time.perf_counter() - started)
                raise

        if tier.needs_confirmation:
            if not caller.interactive:
                detail = "needs your approval; ask in the panel so a confirm card can be shown"
                self._audit.record(
                    actor=caller.label, kind="tool", action=action, tier=tier.value,
                    outcome="denied", arguments=arguments, detail=detail,
                )  # fmt: skip
                raise ToolDenied(f"{action} {detail}")
            approved = await self._confirm.request(
                plugin=plugin_id, tool=tool, arguments=arguments,
                actor=caller.label, session=caller.session,
            )  # fmt: skip
            if not approved:
                self._audit.record(
                    actor=caller.label, kind="tool", action=action, tier=tier.value,
                    outcome="declined", arguments=arguments,
                )  # fmt: skip
                self._telemetry.tool_call(
                    plugin_id, tool, "declined", time.perf_counter() - started
                )
                raise ToolDenied(f"you declined {action}; do not retry it")

        plugin = self._plugins.get(plugin_id)
        outcome, detail = "ok", ""
        for_chat = caller.interactive or current_priority() == CHAT
        if for_chat:
            self._for_chat[plugin_id] += 1
        try:
            with self._telemetry.span(
                "tool.call", {"bob.plugin": plugin_id, "bob.tool": tool, "bob.tier": tier.value}
            ):
                if plugin.client is None:
                    raise ToolDenied(f"plugin {plugin_id!r} is switched off")
                result = await plugin.client.call_tool(tool, arguments)
            if result.is_error:
                raise ToolFailed(_error_text(result))
            data = parse_result(result)
        except (ToolDenied, ToolFailed) as exc:
            outcome, detail = "error", str(exc)
            raise
        except Exception as exc:
            outcome, detail = "error", f"{exc.__class__.__name__}: {exc}"
            log.exception("tool %s crashed", action)
            raise ToolFailed(f"{action} failed: {exc.__class__.__name__}") from exc
        finally:
            if for_chat:
                self._for_chat[plugin_id] -= 1
            # Every external call is audited; so is everything not asked by a model's
            # routine reads, to keep the log useful without drowning it.
            if tier is not Tier.READ or outcome != "ok" or caller.kind != "model":
                self._audit.record(
                    actor=caller.label, kind="tool", action=action, tier=tier.value,
                    outcome=outcome, arguments=arguments, detail=detail,
                )  # fmt: skip
            self._telemetry.tool_call(plugin_id, tool, outcome, time.perf_counter() - started)

        if tier is not Tier.READ and self._events is not None:
            # Something changed: the widget and the menu bar refresh their cards.
            await self._events.publish(
                "tool.finished", plugin=plugin_id, tool=tool, tier=tier.value
            )
        if plugin_id == KNOWLEDGE_PLUGIN and tool == "list_collections" and caller.plugin:
            data = self._filter_collections(data, caller)
        return data

    def _filter_collections(self, data: Any, caller: Caller) -> Any:
        if caller.kind not in ("model", "plugin") or not isinstance(data, dict):
            return data
        scope = self._plugins.get(caller.plugin or "").manifest.knowledge
        visible = [c for c in data.get("collections", []) if scope.may_read(str(c.get("id")))]
        return {**data, "collections": visible}
