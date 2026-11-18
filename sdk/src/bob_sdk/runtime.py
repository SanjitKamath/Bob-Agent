"""What a plugin's MCP server needs at run time.

The core starts each plugin server over stdio and hands it one JSON file (its
path is in ``$BOB_PLUGIN_CONFIG``) with the plugin's settings, its private data
folder and how to reach the core. A server run by hand, for example under the
MCP Inspector, gets sensible standalone defaults instead.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .manifest import MANIFEST_FILENAME, Manifest, load_manifest
from .ollama import DEFAULT_BASE_URL, ModelSpec, OllamaClient
from .output import OutputFolder

CONFIG_ENV = "BOB_PLUGIN_CONFIG"

__all__ = [
    "CONFIG_ENV",
    "CoreClient",
    "CoreUnavailable",
    "PluginContext",
    "ToolError",
    "create_server",
    "manifest_for",
]


class CoreUnavailable(ToolError):
    """The plugin needs the core (bobd) but is running without it."""


def manifest_for(module_file: str) -> Manifest:
    """Load the ``plugin.yaml`` that sits next to a plugin module."""
    return load_manifest(Path(module_file).resolve().parent / MANIFEST_FILENAME)


class CoreClient:
    """How a plugin asks the core for something outside its own boundary.

    Plugins never talk to each other directly. A call to another plugin's tool
    goes through the core, which checks this plugin's manifest first.
    """

    def __init__(self, url: str | None, token: str | None, *, timeout_s: float = 600.0) -> None:
        self._url = url.rstrip("/") if url else None
        self._token = token
        self._timeout = timeout_s

    @property
    def available(self) -> bool:
        return bool(self._url and self._token)

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        if not self.available:
            raise CoreUnavailable("this tool needs bobd; start the plugin through Bob")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as http:
                response = await http.post(
                    f"{self._url}/internal/{path}",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
        except httpx.HTTPError as exc:
            raise CoreUnavailable(f"cannot reach bobd: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise ToolError(str(detail))
        return response.json()

    async def call_tool(self, plugin: str, tool: str, arguments: dict[str, Any]) -> Any:
        """Call another plugin's tool through the core's permission checks."""
        data = await self._post("call", {"plugin": plugin, "tool": tool, "arguments": arguments})
        return data["result"]

    async def summarize_untrusted(self, text: str, *, kind: str = "email") -> dict[str, Any]:
        """Summarise untrusted text in a model call that has no tools and a fixed schema."""
        return await self._post("summarize", {"text": text, "kind": kind})

    async def generate(self, role: str, system: str, prompt: str) -> str:
        """Plain text generation with one of the configured model roles."""
        data = await self._post("generate", {"role": role, "system": system, "prompt": prompt})
        return str(data["text"])

    async def audit(self, action: str, arguments: dict[str, Any], outcome: str) -> None:
        """Record something this plugin did (for example a git command) in the audit log."""
        await self._post("audit", {"action": action, "arguments": arguments, "outcome": outcome})

    async def publish(self, topic: str, data: dict[str, Any] | None = None) -> None:
        await self._post("publish", {"topic": topic, "data": data or {}})


@dataclass
class PluginContext:
    plugin_id: str
    manifest: Manifest
    settings: dict[str, Any]
    data_dir: Path
    core: CoreClient
    ollama_url: str = DEFAULT_BASE_URL
    models: dict[str, ModelSpec] = field(default_factory=dict)
    # How many requests each model role may run at once, as set in Settings → Models.
    model_limits: dict[str, int] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)  # plugin-specific configuration

    @classmethod
    def load(cls, manifest: Manifest) -> PluginContext:
        config_path = os.environ.get(CONFIG_ENV)
        raw: dict[str, Any] = {}
        if config_path:
            raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
        settings = {**manifest.default_settings(), **raw.get("settings", {})}
        default_data = Path.home() / "Library" / "Application Support" / "Bob" / "plugins"
        data_dir = Path(raw.get("data_dir") or default_data / manifest.id).expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            plugin_id=manifest.id,
            manifest=manifest,
            settings=settings,
            data_dir=data_dir,
            core=CoreClient(raw.get("core_url"), raw.get("token")),
            ollama_url=raw.get("ollama_url", DEFAULT_BASE_URL),
            models={
                role: ModelSpec.model_validate(spec) for role, spec in raw.get("models", {}).items()
            },
            model_limits={
                str(role): int(limit)
                for role, limit in (raw.get("model_limits") or {}).items()
                if isinstance(limit, int) and not isinstance(limit, bool)
            },
            extra=raw.get("extra", {}),
        )

    def setting(self, key: str) -> Any:
        if key not in self.manifest.settings:
            raise KeyError(f"plugin {self.plugin_id!r} declares no setting {key!r}")
        return self.settings.get(key)

    def output_folder(self, setting_key: str) -> OutputFolder:
        """The validated output folder behind a ``permissions.write`` setting."""
        if setting_key not in self.manifest.write_setting_keys:
            raise ToolError(f"setting {setting_key!r} is not a declared output folder")
        value = self.setting(setting_key)
        if not value:
            raise ToolError(f"output folder setting {setting_key!r} is empty")
        try:
            return OutputFolder(value)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    def ollama(self) -> OllamaClient:
        return OllamaClient(self.ollama_url)

    def model(self, role: str) -> ModelSpec:
        if role not in self.models:
            raise ToolError(f"model role {role!r} is not configured")
        return self.models[role]


def create_server(
    manifest: Manifest, *, instructions: str | None = None, lifespan: Any = None
) -> MCPServer:
    """Create the plugin's MCP server, named after its manifest."""
    return MCPServer(
        lifespan=lifespan,
        name=manifest.id,
        title=manifest.name,
        version=manifest.version,
        instructions=instructions or manifest.description or None,
        log_level="WARNING",
    )


def check_declared(server: MCPServer, manifest: Manifest) -> None:
    """Fail fast if the server and the manifest disagree about the tool list."""
    import asyncio

    offered = {tool.name for tool in asyncio.run(server.list_tools())}
    declared = set(manifest.tools)
    if offered != declared:
        undeclared = sorted(offered - declared)
        missing = sorted(declared - offered)
        raise RuntimeError(
            f"plugin {manifest.id!r}: tools and manifest disagree "
            f"(undeclared: {undeclared}, declared but missing: {missing})"
        )


def run(server: MCPServer, manifest: Manifest) -> None:
    """Entry point for ``python -m <plugin>.server``: check the tool list, then serve stdio."""
    check_declared(server, manifest)
    server.run("stdio")
