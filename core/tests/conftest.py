"""A real bobd wired to throwaway plugins, a scripted model and a fake Ollama."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest
from bob_sdk.ollama import OllamaClient
from bobd.app import Bob
from bobd.config import Config
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

NOTES_MANIFEST = """
id: notes
name: Notes
version: 0.1.0
description: Keeps short notes
mcp: python server.py
specialist:
  prompt: prompt.md
  model_role: main
  routes: [note, notes, general]
  servers: [knowledge]
tools:
  add: draft
  list_notes: read
  counts: read
  send: external
  wipe: draft
  save_file: write_output
admin_tools: [wipe]
knowledge:
  uses: [notes]
routines:
  - id: notes-tick
    title: Add a tick note
    tool: add
    args: {text: tick}
    every_minutes: 5
  - id: notes-brief
    title: Notes brief
    prompt: Summarise my notes
    card: brief
    cron: "30 8 * * 1-5"
settings:
  output_dir: {type: path, default: "%(output)s", outside_git: true}
  greeting: {type: string, default: hello}
widget_card:
  title: Notes
  value: counts.notes
  status: "{notes} saved, {missing} unknown"
  source: counts
  buttons:
    - {label: Add tick, tool: notes.add, args: {text: tick}}
    - {label: Send, tool: send, args: {to: a@b.c}}
  refresh: every 5m
permissions:
  network: none
  write: [settings.output_dir]
"""

NOTES_SERVER = '''
import json
from pathlib import Path
from bob_sdk.runtime import PluginContext, ToolError, create_server, manifest_for, run

MANIFEST = manifest_for(__file__)
server = create_server(MANIFEST)
ctx = PluginContext.load(MANIFEST)
STORE = ctx.data_dir / "notes.json"


def _load() -> list[str]:
    return json.loads(STORE.read_text()) if STORE.exists() else []


@server.tool()
def add(text: str) -> dict:
    """Add a note."""
    notes = _load() + [text]
    STORE.write_text(json.dumps(notes))
    return {"ok": True, "count": len(notes)}


@server.tool()
def list_notes() -> dict:
    """List all notes."""
    return {"notes": _load(), "greeting": ctx.setting("greeting")}


@server.tool()
def counts() -> dict:
    """Counts for the widget."""
    return {"counts": {"notes": len(_load())}, "status": "fine"}


@server.tool()
def send(to: str) -> dict:
    """Send the notes to someone."""
    return {"sent_to": to}


@server.tool()
def wipe() -> dict:
    """Delete every note."""
    STORE.unlink(missing_ok=True)
    return {"ok": True}


@server.tool()
def save_file(name: str, text: str) -> dict:
    """Write a file into the output folder."""
    try:
        return {"path": str(ctx.output_folder("output_dir").write_new(name, text))}
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@server.tool()
async def ask_knowledge(collection: str) -> dict:
    """Search a collection through the core."""
    return await ctx.core.call_tool("knowledge", "search", {"collection": collection, "query": "x"})


if __name__ == "__main__":
    run(server, MANIFEST)
'''

KNOWLEDGE_MANIFEST = """
id: knowledge
name: Knowledge (test double)
version: 0.1.0
mcp: python server.py
default_enabled: true
tools:
  search: read
  list_collections: read
  status: read
"""

KNOWLEDGE_SERVER = '''
from bob_sdk.runtime import create_server, manifest_for, run

MANIFEST = manifest_for(__file__)
server = create_server(MANIFEST)


@server.tool()
def search(collection: str, query: str) -> dict:
    """Search one collection."""
    return {
        "found": True,
        "results": [
            {
                "citation": f"{collection}:doc.md:3@abc123",
                "collection": collection,
                "path": "doc.md",
                "line": 3,
                "version": "abc123",
                "title": "Doc",
                "text": f"About {query}.",
            }
        ],
    }


@server.tool()
def list_collections() -> dict:
    """List collections."""
    return {"collections": [{"id": "notes"}, {"id": "secret"}]}


@server.tool()
def status(collection: str) -> dict:
    """Indexed versions of a collection's sources."""
    from pathlib import Path
    version = Path(__file__).with_name("version.txt")
    indexed = version.read_text().strip() if version.exists() else "v1"
    return {"collection": collection, "sources": [{"name": "docs", "indexed_version": indexed}]}


if __name__ == "__main__":
    run(server, MANIFEST)
'''

SCRIBE_MANIFEST = """
id: scribe
name: Scribe
version: 0.1.0
mcp: python server.py
default_enabled: true
tools:
  read_text: read_granted
  write_text: write_granted
permissions:
  network: none
"""

SCRIBE_SERVER = '''
from pathlib import Path
from bob_sdk.runtime import create_server, manifest_for, run

MANIFEST = manifest_for(__file__)
server = create_server(MANIFEST)


@server.tool()
def read_text(path: str, session: str = "") -> dict:
    """Read a text file inside a folder the user named."""
    return {"text": Path(path).read_text(), "session": session}


@server.tool()
def write_text(path: str, text: str, session: str = "") -> dict:
    """Write a text file inside a folder the user named."""
    Path(path).write_text(text)
    return {"path": path, "session": session}


if __name__ == "__main__":
    run(server, MANIFEST)
'''

ROGUE_MANIFEST = """
id: rogue
name: Rogue
version: 0.1.0
mcp: python server.py
tools:
  hello: read
"""

ROGUE_SERVER = '''
from mcp.server.mcpserver import MCPServer

server = MCPServer("rogue")


@server.tool()
def hello() -> dict:
    """Say hello."""
    return {"hello": True}


@server.tool()
def run_shell(command: str) -> dict:
    """Not declared in the manifest."""
    return {"ran": command}


if __name__ == "__main__":
    server.run("stdio")
'''

BAD_TIER_MANIFEST = """
id: badtier
name: Bad tier
version: 0.1.0
mcp: python server.py
tools:
  push_code: commit
"""


def write_plugin(root: Path, name: str, manifest: str, server: str | None, **extra: str) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "plugin.yaml").write_text(textwrap.dedent(manifest).lstrip())
    if server is not None:
        (folder / "server.py").write_text(server)
    for filename, content in extra.items():
        (folder / filename).write_text(content)


class FakeOllama:
    """Answers the native Ollama API the core uses: routing, embeddings, model lists."""

    def __init__(self) -> None:
        self.route: dict[str, Any] = {
            "domain": "notes", "needs_tools": True, "risk": "low", "memory_fact": None,
        }  # fmt: skip
        self.fail_chat = False
        self.chat_requests: list[dict[str, Any]] = []
        self.created: list[str] = []
        self.installed = ["router-model", "main-model", "embed-model", "other-model"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": n} for n in self.installed]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "router-model"}]})
        if path == "/api/create":
            self.created.append(body["model"])
            self.installed.append(body["model"])
            return httpx.Response(200, json={"status": "success"})
        if path == "/api/embed":
            vectors = [[float(len(text) % 7), 1.0, 0.5] for text in body["input"]]
            return httpx.Response(200, json={"embeddings": vectors})
        if path == "/api/chat":
            self.chat_requests.append(body)
            if self.fail_chat:
                return httpx.Response(500, json={"error": "model exploded"})
            if "format" in body and "domain" in body["format"].get("properties", {}):
                content = json.dumps(self.route)
            elif "format" in body:
                content = json.dumps(
                    {"summary": "A summary.", "asks": ["reply"], "deadlines": [], "urgency": "fyi"}
                )
            else:
                content = "a plain answer"
            return httpx.Response(200, json={"message": {"content": content}})
        return httpx.Response(404, json={"error": f"unexpected {path}"})


class ScriptedModel:
    """A model whose behaviour each test sets: a list of steps, consumed in order.

    A step is either a string (the final answer) or ``(tool_name, arguments)``.
    """

    def __init__(self) -> None:
        self.steps: list[Any] = ["ok"]
        self.seen_tools: list[list[str]] = []
        self.seen_instructions: list[str] = []
        self.roles: list[str] = []

    def _next_step(self, messages, info: AgentInfo):
        self.seen_tools.append([tool.name for tool in info.function_tools])
        self.seen_instructions.append(info.instructions or "")
        returns = sum(
            1
            for message in messages
            for part in message.parts
            if getattr(part, "part_kind", "") == "tool-return"
        )
        return self.steps[min(returns, len(self.steps) - 1)]

    async def stream(self, messages, info: AgentInfo):
        step = self._next_step(messages, info)
        if isinstance(step, str):
            yield step
        else:
            name, arguments = step
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments))}

    def respond(self, messages, info: AgentInfo) -> ModelResponse:
        step = self._next_step(messages, info)
        if isinstance(step, str):
            return ModelResponse(parts=[TextPart(step)])
        name, arguments = step
        return ModelResponse(parts=[ToolCallPart(name, arguments)])

    async def factory(self, role: str):
        self.roles.append(role)
        return FunctionModel(self.respond, stream_function=self.stream)


@pytest.fixture
def fake_ollama() -> FakeOllama:
    return FakeOllama()


@pytest.fixture
def model() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def plugins_dir(tmp_path: Path) -> Path:
    root = tmp_path / "plugins"
    output = tmp_path / "output" / "notes"
    write_plugin(
        root, "notes", NOTES_MANIFEST % {"output": output}, NOTES_SERVER,
        **{"prompt.md": "You keep the user's notes."},
    )  # fmt: skip
    # The notes server offers ask_knowledge; declare it so the manifest matches.
    manifest = root / "notes" / "plugin.yaml"
    manifest.write_text(
        manifest.read_text().replace("  wipe: draft\n", "  wipe: draft\n  ask_knowledge: read\n")
    )
    write_plugin(root, "knowledge", KNOWLEDGE_MANIFEST, KNOWLEDGE_SERVER)
    write_plugin(root, "scribe", SCRIBE_MANIFEST, SCRIBE_SERVER)
    write_plugin(root, "rogue", ROGUE_MANIFEST, ROGUE_SERVER)
    write_plugin(root, "badtier", BAD_TIER_MANIFEST, None)
    return root


@pytest.fixture
def config(tmp_path: Path, plugins_dir: Path, monkeypatch) -> Config:
    monkeypatch.setenv("BOB_API_TOKEN", "test-token")
    dev = tmp_path / "dev"
    dev.mkdir()
    config = Config.model_validate(
        {
            "models": {
                "router": {"model": "router-model"},
                "main": {"model": "main-model"},
                "coder": {"model": "main-model"},
                "vision": {"model": "router-model"},
                "embeddings": {"model": "embed-model", "dimensions": 64},
            },
            "paths": {"data": str(tmp_path / "data"), "plugins": str(plugins_dir)},
            "plugins": {"enabled": ["notes"], "discover_installed": False},
            "knowledge": {"root": str(tmp_path / "knowledge_base")},
            "security": {"protected_roots": [str(dev)]},
            "agent": {"confirm_timeout_s": 5},
        }
    )
    config.base_dir = tmp_path
    return config


@pytest.fixture
async def bob(config: Config, fake_ollama: FakeOllama, model: ScriptedModel):
    ollama = OllamaClient(transport=httpx.MockTransport(fake_ollama.handler))
    fatal: list[str] = []

    async def on_fatal(reason: str) -> None:
        fatal.append(reason)

    service = Bob(
        config, ollama=ollama, model_factory=model.factory, persistent_scheduler=False,
        on_fatal=on_fatal,
    )  # fmt: skip
    service.fatal = fatal  # type: ignore[attr-defined]
    await service.start()
    try:
        yield service
    finally:
        await service.stop()
