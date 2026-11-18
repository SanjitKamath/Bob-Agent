"""The plugin manager: discover, validate, ask, start, register, disable.

The core never imports plugin code. A plugin is a manifest plus an MCP server
that the manager starts over stdio and talks to as a client.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bob_sdk.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestError,
    Tier,
    coerce_setting,
    load_manifest,
)
from bob_sdk.output import OutputFolderError, expand, validate_output_dir
from mcp import Client, StdioServerParameters
from mcp.types import Tool as McpTool

from .audit import AuditLog
from .config import Config
from .db import Database
from .events import EventBus

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "bob.plugins"
_SCOPE = "plugins"
START_TIMEOUT_S = 60.0

ExtraProvider = Callable[["PluginManager"], dict[str, Any]]


class PluginError(Exception):
    """A plugin could not be loaded, started or configured."""


class ConsentRequired(PluginError):
    """First-time enable: the app must show what the plugin can do and get a yes."""

    def __init__(self, summary: dict[str, Any]) -> None:
        super().__init__(f"plugin {summary['id']!r} needs your approval before first use")
        self.summary = summary


@dataclass
class Plugin:
    manifest: Manifest
    folder: Path  # the folder holding plugin.yaml
    status: str = "stopped"  # stopped | starting | running | error
    error: str = ""
    token: str | None = None
    client: Client | None = None
    mcp_tools: dict[str, McpTool] = field(default_factory=dict)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _stop: asyncio.Event | None = field(default=None, repr=False)

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def running(self) -> bool:
        return self.status == "running" and self.client is not None


def discover(
    plugins_dir: Path, *, installed: bool = True
) -> tuple[dict[str, tuple[Manifest, Path]], dict[str, str]]:
    """Find plugins in the plugins folder and in installed ``bob.plugins`` packages.

    Returns the valid plugins and, per rejected location, why it was rejected.
    """
    found: dict[str, tuple[Manifest, Path]] = {}
    rejected: dict[str, str] = {}

    def consider(manifest_file: Path) -> None:
        try:
            manifest = load_manifest(manifest_file)
        except ManifestError as exc:
            rejected[str(manifest_file)] = str(exc)
            return
        found.setdefault(manifest.id, (manifest, manifest_file.parent))

    if plugins_dir.is_dir():
        candidates = [
            *sorted(plugins_dir.glob(f"*/{MANIFEST_FILENAME}")),
            *sorted(plugins_dir.glob(f"*/src/*/{MANIFEST_FILENAME}")),
        ]
        for manifest_file in candidates:
            consider(manifest_file)
    entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP) if installed else ()
    for entry_point in entry_points:
        try:
            spec = importlib.util.find_spec(entry_point.value.split(":")[0])
        except (ImportError, ValueError):
            spec = None
        if spec is None or not spec.origin:
            rejected[f"entry point {entry_point.name}"] = "package is not importable"
            continue
        manifest_file = Path(spec.origin).parent / MANIFEST_FILENAME
        if manifest_file.is_file():
            consider(manifest_file)
        else:
            rejected[f"entry point {entry_point.name}"] = f"no {MANIFEST_FILENAME} in the package"
    return found, rejected


def _describe(exc: BaseException) -> str:
    """A readable message, also for the exception groups task groups raise."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(inner) for inner in exc.exceptions)
    return str(exc) or exc.__class__.__name__


class PluginManager:
    def __init__(
        self,
        config: Config,
        db: Database,
        events: EventBus,
        audit: AuditLog,
        *,
        core_url: str,
        models: Callable[[], dict[str, Any]],
        model_limits: Callable[[], dict[str, int]] | None = None,
    ) -> None:
        self._config = config
        self._db = db
        self._events = events
        self._audit = audit
        self._core_url = core_url
        self._models = models
        self._model_limits = model_limits or (lambda: {})
        self._extra: dict[str, ExtraProvider] = {}
        self._lock = asyncio.Lock()
        self.plugins: dict[str, Plugin] = {}
        self.rejected: dict[str, str] = {}

    # -- discovery and state --------------------------------------------------

    def use_user_site(self) -> None:
        """Make plugins added from wheel files importable, here and in each plugin's process."""
        folder = self._config.user_site_dir
        if folder is not None:
            folder.mkdir(parents=True, exist_ok=True)
            if str(folder) not in sys.path:
                sys.path.insert(0, str(folder))
            importlib.invalidate_caches()

    def discover(self) -> None:
        self.use_user_site()
        found, self.rejected = discover(
            self._config.plugins_dir, installed=self._config.plugins.discover_installed
        )
        for plugin_id, (manifest, folder) in found.items():
            existing = self.plugins.get(plugin_id)
            if existing is None:
                self.plugins[plugin_id] = Plugin(manifest, folder)
            elif not existing.running:
                existing.manifest, existing.folder = manifest, folder
        for location, reason in self.rejected.items():
            log.warning("plugin rejected: %s: %s", location, reason)

    def get(self, plugin_id: str) -> Plugin:
        try:
            return self.plugins[plugin_id]
        except KeyError:
            raise PluginError(f"unknown plugin {plugin_id!r}") from None

    def set_extra_provider(self, plugin_id: str, provider: ExtraProvider) -> None:
        """Register plugin-specific configuration handed to that plugin at start."""
        self._extra[plugin_id] = provider

    def enabled_ids(self) -> list[str]:
        stored = self._db.get_setting(_SCOPE, "enabled")
        if stored is None:
            # First run: what config.yaml lists, plus plugins that are on by default.
            stored = sorted(
                {
                    *self._config.plugins.enabled,
                    *(p.id for p in self.plugins.values() if p.manifest.default_enabled),
                }
            )
            self._db.set_setting(_SCOPE, "enabled", stored)
            self._db.set_setting(_SCOPE, "acknowledged", stored)
        return [plugin_id for plugin_id in stored if plugin_id in self.plugins]

    def running(self) -> list[Plugin]:
        return [plugin for plugin in self.plugins.values() if plugin.running]

    def by_token(self, token: str) -> Plugin | None:
        for plugin in self.plugins.values():
            if plugin.token and secrets.compare_digest(plugin.token, token):
                return plugin
        return None

    # -- settings -----------------------------------------------------------------

    def settings(self, plugin_id: str) -> dict[str, Any]:
        """Effective settings: manifest defaults, then config.yaml, then Settings."""
        manifest = self.get(plugin_id).manifest
        values = manifest.default_settings()
        values.update(self._config.plugins.settings.get(plugin_id, {}))
        values.update(self._db.get_settings(f"plugin:{plugin_id}"))
        return {key: values.get(key) for key in manifest.settings}

    def _check_settings(self, manifest: Manifest, values: dict[str, Any]) -> dict[str, Any]:
        checked: dict[str, Any] = {}
        for key, value in values.items():
            spec = manifest.settings.get(key)
            if spec is None:
                raise PluginError(f"plugin {manifest.id!r} has no setting {key!r}")
            try:
                value = coerce_setting(key, spec, value)
            except ManifestError as exc:
                raise PluginError(str(exc)) from exc
            if spec.type == "path" and spec.outside_git and value:
                try:
                    validate_output_dir(value, self._protected_roots())
                except OutputFolderError as exc:
                    raise PluginError(f"setting {key!r}: {exc}") from exc
            checked[key] = value
        return checked

    def _protected_roots(self) -> list[Path]:
        roots = [expand(root) for root in self._config.security.protected_roots]
        roots.append(self._config.knowledge_root)
        return roots

    async def update_settings(self, plugin_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Change settings. They apply immediately: a running plugin is restarted."""
        plugin = self.get(plugin_id)
        checked = self._check_settings(plugin.manifest, values)
        for key, value in checked.items():
            if plugin.manifest.settings[key].secret:
                raise PluginError(f"setting {key!r} is a secret; store it in the Keychain")
            self._db.set_setting(f"plugin:{plugin_id}", key, value)
        self._audit.record(
            actor="user", kind="settings", action=f"{plugin_id}.settings", outcome="ok",
            arguments=checked,
        )  # fmt: skip
        if plugin.running:
            await self.restart(plugin_id)
        return self.settings(plugin_id)

    def disabled_tools(self, plugin_id: str) -> set[str]:
        stored = self._db.get_setting(_SCOPE, f"disabled_tools:{plugin_id}")
        if stored is None:
            stored = self._config.plugins.disabled_tools.get(plugin_id, [])
        return set(stored)

    async def set_tool_enabled(self, plugin_id: str, tool: str, enabled: bool) -> None:
        plugin = self.get(plugin_id)
        if tool not in plugin.manifest.tools:
            raise PluginError(f"plugin {plugin_id!r} has no tool {tool!r}")
        disabled = self.disabled_tools(plugin_id)
        disabled.discard(tool) if enabled else disabled.add(tool)
        self._db.set_setting(_SCOPE, f"disabled_tools:{plugin_id}", sorted(disabled))
        await self._events.publish("plugin.tools_changed", plugin=plugin_id)

    # -- what the "Ask" step shows -------------------------------------------------

    def summary(self, plugin_id: str) -> dict[str, Any]:
        plugin = self.get(plugin_id)
        manifest = plugin.manifest
        settings = self.settings(plugin_id)
        disabled = self.disabled_tools(plugin_id)
        return {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "status": plugin.status,
            "error": plugin.error,
            "enabled": plugin_id in self.enabled_ids(),
            "default_enabled": manifest.default_enabled,
            "tools": [
                {
                    "name": name,
                    "tier": tier.value,
                    "asks_first": tier.needs_confirmation,
                    "admin": name in manifest.admin_tools,
                    "enabled": name not in disabled,
                }
                for name, tier in manifest.tools.items()
            ],
            "specialist": None
            if manifest.specialist is None
            else {
                "name": manifest.specialist.name or manifest.name,
                "model_role": manifest.specialist.model_role,
                "routes": manifest.specialist.routes,
                "servers": manifest.specialist.servers,
            },
            "knowledge": manifest.knowledge.model_dump(),
            "routines": [routine.id for routine in manifest.routines],
            "network": self._network(manifest, settings),
            "write_folders": [settings.get(key) for key in manifest.write_setting_keys],
            "settings": settings,
            "settings_schema": manifest.settings_json_schema(),
            "widget_card": None
            if manifest.widget_card is None
            else manifest.widget_card.model_dump(),
        }

    @staticmethod
    def _network(manifest: Manifest, settings: dict[str, Any]) -> str | list[str]:
        """Hosts the plugin may reach; ``settings.<name>`` entries show their current value."""
        if manifest.permissions.network == "none":
            return "none"
        hosts = []
        for entry in manifest.permissions.network:
            if entry.startswith("settings."):
                hosts.append(str(settings.get(entry.split(".", 1)[1]) or f"({entry}, not set)"))
            else:
                hosts.append(entry)
        return hosts

    # -- lifecycle --------------------------------------------------------------------

    async def start_enabled(self) -> None:
        for plugin_id in self.enabled_ids():
            try:
                await self._start(self.get(plugin_id))
            except PluginError as exc:
                log.error("plugin %s did not start: %s", plugin_id, exc)

    async def enable(self, plugin_id: str, *, acknowledged: bool = False) -> dict[str, Any]:
        plugin = self.get(plugin_id)
        known = set(self._db.get_setting(_SCOPE, "acknowledged", []))
        if plugin_id not in known:
            if not acknowledged:
                raise ConsentRequired(self.summary(plugin_id))
            self._db.set_setting(_SCOPE, "acknowledged", sorted(known | {plugin_id}))
        async with self._lock:
            enabled = self.enabled_ids()
            if plugin_id not in enabled:
                self._db.set_setting(_SCOPE, "enabled", sorted({*enabled, plugin_id}))
            await self._start(plugin)
        self._audit.record(actor="user", kind="plugin", action=f"enable {plugin_id}", outcome="ok")
        await self._after_change(plugin_id, "plugin.enabled")
        return self.summary(plugin_id)

    async def disable(self, plugin_id: str) -> dict[str, Any]:
        """Stop the server and remove its tools at once. Data and settings are kept."""
        plugin = self.get(plugin_id)
        async with self._lock:
            enabled = [item for item in self.enabled_ids() if item != plugin_id]
            self._db.set_setting(_SCOPE, "enabled", enabled)
            await self._stop(plugin)
        self._audit.record(actor="user", kind="plugin", action=f"disable {plugin_id}", outcome="ok")
        await self._after_change(plugin_id, "plugin.disabled")
        return self.summary(plugin_id)

    async def restart(self, plugin_id: str) -> None:
        plugin = self.get(plugin_id)
        async with self._lock:
            await self._stop(plugin)
            await self._start(plugin)
        await self._events.publish("plugin.restarted", plugin=plugin_id)

    async def reload(self, plugin_id: str) -> bool:
        """Start a running plugin again so it runs the code that was just installed.

        A plugin process keeps the code it started with; without this an updated wheel would
        only take effect at the next restart of Bob. Returns True when it was running.
        """
        plugin = self.plugins.get(plugin_id)
        if plugin is None or not plugin.running:
            return False
        await self.restart(plugin_id)
        # Plugins that learn parsers or settings from others (Knowledge) see the new code too.
        for dependent in self._extra:
            other = self.plugins.get(dependent)
            if dependent != plugin_id and other is not None and other.running:
                await self.restart(dependent)
        return True

    async def _after_change(self, plugin_id: str, topic: str) -> None:
        # Plugins that take configuration from others (the Knowledge plugin learns
        # which parsers and collections exist) are restarted so they see the change.
        for dependent in self._extra:
            other = self.plugins.get(dependent)
            if dependent != plugin_id and other is not None and other.running:
                await self.restart(dependent)
        await self._events.publish(topic, plugin=plugin_id)

    async def stop_all(self) -> None:
        async with self._lock:
            for plugin in self.plugins.values():
                await self._stop(plugin)

    def _write_config(self, plugin: Plugin) -> Path:
        folder = self._config.data_dir / "plugins" / plugin.id
        data_dir = folder / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        settings = self._check_settings(plugin.manifest, self.settings(plugin.id))
        for key in plugin.manifest.write_setting_keys:
            if not settings.get(key):
                raise PluginError(f"output folder setting {key!r} is not set")
        plugin.token = secrets.token_urlsafe(32)
        payload = {
            "plugin_id": plugin.id,
            "settings": settings,
            "data_dir": str(data_dir),
            "core_url": self._core_url,
            "token": plugin.token,
            "ollama_url": self._config.ollama.base_url,
            "models": self._models(),
            # How many requests each role may run at once (Settings → Models).
            "model_limits": self._model_limits(),
            "extra": self._extra[plugin.id](self) if plugin.id in self._extra else {},
        }
        path = folder / "config.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def _server_parameters(self, plugin: Plugin, config_path: Path) -> StdioServerParameters:
        command = plugin.manifest.command
        if command[0] in {"python", "python3"}:
            command = [sys.executable, *command[1:]]
        env = {
            "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),  # noqa: S108
            "BOB_PLUGIN_CONFIG": str(config_path),
            "BOB_PROTECTED_ROOTS": os.pathsep.join(str(root) for root in self._protected_roots()),
            "PYTHONUNBUFFERED": "1",
        }
        if (user_site := self._config.user_site_dir) is not None:
            env["PYTHONPATH"] = str(user_site)
        return StdioServerParameters(
            command=command[0], args=command[1:], env=env, cwd=str(plugin.folder)
        )

    async def _start(self, plugin: Plugin) -> None:
        if plugin.running:
            return
        plugin.status, plugin.error = "starting", ""
        try:
            parameters = self._server_parameters(plugin, self._write_config(plugin))
        except PluginError as exc:
            plugin.status, plugin.error = "error", str(exc)
            raise
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        plugin._stop = asyncio.Event()
        plugin._task = asyncio.create_task(self._serve(plugin, parameters, ready))
        try:
            await asyncio.wait_for(ready, START_TIMEOUT_S)
        except Exception as exc:
            await self._stop(plugin)
            message = "start timed out" if isinstance(exc, TimeoutError) else str(exc)
            plugin.status, plugin.error = "error", message
            self._audit.record(
                actor="core", kind="plugin", action=f"start {plugin.id}", outcome="error",
                detail=message,
            )  # fmt: skip
            raise PluginError(f"plugin {plugin.id!r} failed to start: {message}") from exc
        log.info("plugin %s running with %d tool(s)", plugin.id, len(plugin.mcp_tools))

    async def _serve(
        self, plugin: Plugin, parameters: StdioServerParameters, ready: asyncio.Future[None]
    ) -> None:
        """Own the MCP client for the plugin's whole life, in one task."""
        assert plugin._stop is not None
        try:
            problem: str | None = None
            async with Client(parameters, read_timeout_seconds=1800) as client:
                # Health check: the server must offer exactly the declared tools.
                offered = {tool.name: tool for tool in (await client.list_tools()).tools}
                declared = set(plugin.manifest.tools)
                undeclared = sorted(set(offered) - declared)
                missing = sorted(declared - set(offered))
                if undeclared:
                    problem = f"server offers undeclared tool(s): {', '.join(undeclared)}"
                elif missing:
                    problem = f"declared tool(s) missing from server: {', '.join(missing)}"
                else:
                    plugin.client, plugin.mcp_tools, plugin.status = client, offered, "running"
                    if not ready.done():
                        ready.set_result(None)
                    await plugin._stop.wait()
            # Raised outside the client's task group, so the message stays readable.
            if problem is not None:
                raise PluginError(problem)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = _describe(exc)
            if not ready.done():
                ready.set_exception(PluginError(message))
            else:
                log.error("plugin %s stopped unexpectedly: %s", plugin.id, message)
                plugin.status, plugin.error = "error", message
        finally:
            plugin.client, plugin.mcp_tools = None, {}
            if plugin.status == "running":
                plugin.status = "stopped"

    async def _stop(self, plugin: Plugin) -> None:
        task, stop = plugin._task, plugin._stop
        plugin._task = plugin._stop = None
        plugin.client, plugin.mcp_tools, plugin.token = None, {}, None
        if stop is not None:
            stop.set()
        if task is not None:
            try:
                await asyncio.wait_for(task, 10)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except Exception:  # the task already reported its own failure
                pass
        if plugin.status != "error":
            plugin.status = "stopped"

    # -- what the rest of the core asks -------------------------------------------------

    def tier(self, plugin_id: str, tool: str) -> Tier:
        try:
            return self.get(plugin_id).manifest.tools[tool]
        except KeyError:
            raise PluginError(f"plugin {plugin_id!r} declares no tool {tool!r}") from None

    def allowed_parsers(self) -> list[str]:
        """Parser references contributed by enabled plugins."""
        return sorted(
            collection.parser
            for plugin_id in self.enabled_ids()
            for collection in self.plugins[plugin_id].manifest.knowledge.provides
            if collection.parser
        )
