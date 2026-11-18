"""What ``bob plugin test`` runs: schema check, tool smoke test, read-only conformance."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from mcp import Client, StdioServerParameters

from .manifest import MANIFEST_FILENAME, Manifest, ManifestError, load_manifest

_FORBIDDEN_TOOL = re.compile(
    r"(^|_)(shell|exec|execute|run_command|bash|terminal|eval|commit|push|merge|rebase|deploy)($|_)"
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _describe(exc: BaseException) -> str:
    """A readable message, also for the exception groups task groups raise."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(inner) for inner in exc.exceptions)
    return str(exc) or type(exc).__name__


def find_manifest(folder: Path) -> Path | None:
    candidates = [folder / MANIFEST_FILENAME, *sorted(folder.glob(f"src/*/{MANIFEST_FILENAME}"))]
    return next((path for path in candidates if path.is_file()), None)


def conformance(manifest: Manifest) -> list[CheckResult]:
    """The read-only rules a manifest must meet, beyond the schema."""
    bad = sorted(name for name in manifest.tools if _FORBIDDEN_TOOL.search(name))
    return [
        CheckResult("no shell, commit, push or deploy tool", not bad, ", ".join(bad) or "ok"),
        CheckResult(
            "every output folder is outside git",
            all(manifest.settings[key].outside_git for key in manifest.write_setting_keys),
        ),
    ]


async def _offered_tools(manifest: Manifest, folder: Path) -> list[str]:
    command = manifest.command
    if command[0] in {"python", "python3"}:
        command = [sys.executable, *command[1:]]
    with tempfile.TemporaryDirectory(prefix="bob-plugin-test-") as scratch:
        config = Path(scratch) / "config.json"
        config.write_text(json.dumps({"data_dir": str(Path(scratch) / "data")}), encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "BOB_PLUGIN_CONFIG": str(config),
        }
        parameters = StdioServerParameters(
            command=command[0], args=command[1:], env=env, cwd=str(folder)
        )
        async with Client(parameters, read_timeout_seconds=60) as client:
            return sorted(tool.name for tool in (await client.list_tools()).tools)


def check_plugin(folder: Path) -> list[CheckResult]:
    """Run every check on a plugin folder. Stops early if the manifest is invalid."""
    manifest_file = find_manifest(folder)
    if manifest_file is None:
        return [CheckResult("manifest found", False, f"no {MANIFEST_FILENAME} in {folder}")]
    try:
        manifest = load_manifest(manifest_file)
    except ManifestError as exc:
        return [CheckResult("manifest is valid", False, str(exc))]
    results = [CheckResult("manifest is valid", True, f"{manifest.id} {manifest.version}")]
    results += conformance(manifest)
    try:
        offered = asyncio.run(asyncio.wait_for(_offered_tools(manifest, manifest_file.parent), 90))
    except Exception as exc:  # the server failed to start or answer
        message = _describe(exc)
        return [*results, CheckResult("server starts and lists its tools", False, message)]
    results.append(CheckResult("server starts and lists its tools", True, ", ".join(offered)))
    declared = sorted(manifest.tools)
    results.append(
        CheckResult(
            "server offers exactly the declared tools",
            offered == declared,
            "" if offered == declared else f"declared {declared}, offered {offered}",
        )
    )
    if manifest.specialist is not None:
        prompt = manifest_file.parent / manifest.specialist.prompt
        results.append(CheckResult("specialist prompt exists", prompt.is_file(), str(prompt)))
    return results


PLUGIN_YAML = """id: {id}
name: {name}
version: 0.1.0
description: TODO describe what this plugin does.
mcp: python -m {module}.server
specialist:
  prompt: prompts/specialist.md
  model_role: main
  routes: [{id}]
tools:
  hello: read
permissions:
  network: none
"""

SERVER_PY = '''"""MCP server for the {name} plugin."""

from __future__ import annotations

from typing import Any

from bob_sdk.runtime import create_server, manifest_for, run

MANIFEST = manifest_for(__file__)
server = create_server(MANIFEST)


@server.tool()
def hello(name: str = "world") -> dict[str, Any]:
    """Say hello. Replace this with your first real tool."""
    return {{"message": f"Hello, {{name}}"}}


def main() -> None:
    run(server, MANIFEST)


if __name__ == "__main__":
    main()
'''

TEST_PY = """from bob_sdk.runtime import check_declared
from {module} import server


def test_hello():
    assert server.hello("Bob") == {{"message": "Hello, Bob"}}


def test_server_declares_exactly_its_tools():
    check_declared(server.server, server.MANIFEST)
"""

PYPROJECT = """[project]
name = "bob-{id}"
version = "0.1.0"
description = "Bob plugin: {name}."
requires-python = ">=3.12"
dependencies = [
    "bob-plugin-sdk",
]

[project.entry-points."bob.plugins"]
{id} = "{module}"

[build-system]
requires = ["uv_build>=0.13.8,<0.14.0"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "{module}"

[tool.uv.sources]
bob-plugin-sdk = {{ workspace = true }}
"""


def scaffold_plugin(plugins_dir: Path, name: str) -> Path:
    """Create a new plugin folder with a manifest, a server, a prompt and a test."""
    plugin_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not plugin_id or not plugin_id[0].isalpha():
        raise ValueError("a plugin name must start with a letter")
    module = f"bob_{plugin_id.replace('-', '_')}"
    folder = plugins_dir / plugin_id.replace("-", "_")
    if folder.exists():
        raise FileExistsError(f"{folder} already exists")
    values = {"id": plugin_id, "name": name.strip().title(), "module": module}
    package = folder / "src" / module
    (package / "prompts").mkdir(parents=True)
    (folder / "tests").mkdir()
    (folder / "pyproject.toml").write_text(PYPROJECT.format(**values), encoding="utf-8")
    (package / "__init__.py").write_text(f'"""Bob plugin: {values["name"]}."""\n', encoding="utf-8")
    (package / MANIFEST_FILENAME).write_text(PLUGIN_YAML.format(**values), encoding="utf-8")
    (package / "server.py").write_text(SERVER_PY.format(**values), encoding="utf-8")
    (package / "prompts" / "specialist.md").write_text(
        f"You are Bob's {values['name']} specialist. TODO: say what you do and how.\n",
        encoding="utf-8",
    )
    (folder / "tests" / f"test_{module}.py").write_text(TEST_PY.format(**values), encoding="utf-8")
    return folder
