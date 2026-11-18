"""Your private plugins, kept in custom_plugins/ (not in git) and given to Bob as wheel files.

    uv run python deploy/custom_plugins.py list     what is in custom_plugins/
    uv run python deploy/custom_plugins.py test     run each plugin's tests from its source
    uv run python deploy/custom_plugins.py build    build each into custom_plugins/dist/*.whl

Then add a wheel in Bob: Settings > Plugins > Add a plugin, and choose the file. Bob shows what
the plugin is and what it asks for before anything is installed, and a plugin you add is off
until you switch it on. To update one, build again and add the new wheel (it replaces the old).

A plugin here is a normal Bob plugin: a folder with a pyproject.toml, a `bob.plugins` entry
point, and a plugin.yaml in its package. Nothing in custom_plugins/ is part of the shared build.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "custom_plugins"
DIST = FOLDER / "dist"


def plugins() -> list[Path]:
    if not FOLDER.is_dir():
        return []
    return sorted(p for p in FOLDER.iterdir() if (p / "pyproject.toml").is_file())


def run(*command: str, env: dict[str, str] | None = None) -> int:
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, env=env).returncode


def list_plugins() -> int:
    found = plugins()
    for folder in found:
        print(folder.relative_to(ROOT))
    if not found:
        print("custom_plugins/ has no plugins here. Put each plugin's folder in it.")
    return 0


def test() -> int:
    failed = 0
    for folder in plugins():
        tests = folder / "tests"
        if not tests.is_dir():
            print(f"{folder.name}: no tests")
            continue
        # The plugin is run from its source, as an editable install would: its folder on the path.
        path = os.pathsep.join(filter(None, [str(folder / "src"), os.environ.get("PYTHONPATH")]))
        failed |= run("uv", "run", "pytest", str(tests), env={**os.environ, "PYTHONPATH": path})
    return 1 if failed else 0


def build() -> int:
    from bobd.pluginwheel import WheelError, inspect_wheel

    DIST.mkdir(exist_ok=True)
    failed = 0
    for folder in plugins():
        before = set(DIST.glob("*.whl"))
        if run("uv", "build", "--wheel", "--out-dir", str(DIST), str(folder)):
            failed = 1
            continue
        new = sorted(set(DIST.glob("*.whl")) - before) or sorted(DIST.glob(f"*{folder.name}*.whl"))
        for wheel in new:
            try:
                found = inspect_wheel(wheel)
            except WheelError as exc:
                print(f"  {wheel.name}: Bob would refuse it: {exc}")
                failed = 1
                continue
            print(
                f"  {wheel.relative_to(ROOT)}  ->  plugin {found.plugin_id!r}, {len(found.tools)} tools"
            )
            for problem in (*found.warnings, *(f"needs {name}" for name in found.unmet)):
                print(f"    note: {problem}")
    return failed


COMMANDS = {"list": list_plugins, "test": test, "build": build}

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) == 2 else ""
    if command not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    sys.exit(COMMANDS[command]())
