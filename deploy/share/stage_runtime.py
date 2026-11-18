#!/usr/bin/env python3
"""Builds the runtime that ships inside Bob.app for the version you give to other people.

    python3 deploy/share/stage_runtime.py [--out app/runtime-stage] [--python /path/to/python3.12]

Run it on an Apple Silicon Mac with `uv` installed and a network connection (`npm run
dmg:share` does). It makes one folder with:

  python/               Python 3.12 with every package Bob needs installed. Bob's own code is
                        compiled bytecode only: no .py source of bobd, the SDK or any plugin.
  voice/                the Whisper base.en speech model (148 MB, SHA-256 checked), so speaking to
                        Bob works the first time with no download (--no-voice-model leaves it out)
  bobd.sh               starts and stops bobd and Ollama through launchd
  config.template.yaml  the clean config setup fills in
  models.json           the models the setup window offers
  ollama.json           the pinned Ollama download (version, address, SHA-256)

Left out on purpose: your private plugins (custom_plugins/), and anything of the author's own
(knowledge collections, folder paths). The script stops if it finds either in the result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # the workspace root: pyproject.toml, uv.lock

# Workspace packages that ship: (wheel/distribution name, import package).
SHIPPED = [
    ("bobd", "bobd"),
    ("bob-plugin-sdk", "bob_sdk"),
    ("bob-knowledge", "bob_knowledge"),
    ("bob-files", "bob_files"),
    ("bob-tasks", "bob_tasks"),
    ("bob-mail", "bob_mail"),
    ("bob-calendar", "bob_calendar"),
    ("bob-documents", "bob_documents"),
    ("bob-editor", "bob_editor"),
    ("bob-system", "bob_system"),
    ("bob-web", "bob_web"),
    ("bob-architect", "bob_architect"),
    ("bob-voice", "bob_voice"),
]
# The speech model that ships inside the app: the file, where it is from, and its SHA-256.
VOICE_MODEL = (
    "ggml-base.en.bin",
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin",
    "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
)


def bundle_voice_model(stage: Path) -> None:
    """Put the pinned Whisper model in <stage>/voice, refusing anything that does not match."""
    name, url, expected = VOICE_MODEL
    target = stage / "voice"
    target.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    partial = target / (name + ".part")
    print("+ download", url)
    with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as out:  # noqa: S310
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != expected:
        partial.unlink()
        raise SystemExit(f"{name} does not match its pinned SHA-256; not shipping it")
    partial.rename(target / name)


# Must never be in the result: your private plugins (custom_plugins/, whatever is in it) and any
# words you list, one per line, in custom_plugins/forbidden.txt (not in git). "/Users/" always.
CUSTOM = ROOT / "custom_plugins"


def private_packages() -> tuple[str, ...]:
    """The import names of every plugin in custom_plugins/, from its pyproject.toml."""
    names: list[str] = []
    for project in sorted(CUSTOM.glob("*/pyproject.toml")) if CUSTOM.is_dir() else []:
        source = project.parent / "src"
        names += [p.name for p in source.iterdir() if p.is_dir() and not p.name.startswith(".")]
        names.append(project.parent.name)
    return tuple(dict.fromkeys(names))


def forbidden_text() -> tuple[str, ...]:
    listed = CUSTOM / "forbidden.txt"
    extra = listed.read_text(encoding="utf-8").split() if listed.is_file() else []
    return ("/Users/", *extra)


FORBIDDEN_PACKAGES = private_packages()
FORBIDDEN_TEXT = forbidden_text()


def run(*command: str, cwd: Path | None = None) -> str:
    print("+", " ".join(command))
    done = subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True)
    return done.stdout


def models_json(source: Path) -> dict:
    """models.txt as JSON: name, capability letters, sizes."""
    models = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, caps, sizes = line.split("|")
        models.append({"name": name, "caps": caps, "sizes": sizes.split()})
    return {"source": "https://ollama.com/library", "models": models}


def site_packages(python: Path) -> Path:
    out = run(str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])")
    return Path(out.strip())


def to_bytecode(python: Path, package: Path) -> int:
    """Compile every .py in the package next to itself and delete the source. Returns the count.

    Compiled by the shipped Python itself: bytecode only loads on the version that made it."""
    run(str(python), "-m", "compileall", "-b", "-q", "-j", "0", str(package))
    removed = 0
    for source in list(package.rglob("*.py")):
        source.unlink()
        removed += 1
    for cache in list(package.rglob("__pycache__")):
        shutil.rmtree(cache)
    return removed


def check(stage: Path, python: Path, site: Path) -> None:
    """Fail the build if source is left, a forbidden plugin is present, or something personal is."""
    for _dist, package in SHIPPED:
        left = list((site / package).rglob("*.py"))
        if left:
            raise SystemExit(f"source left in {package}: {left[0]}")
    for name in FORBIDDEN_PACKAGES:
        if (site / name).exists():
            raise SystemExit(f"{name} must not ship")
    for path in list(stage.glob("*.*")) + list((site / "bobd").rglob("*.yaml")):
        if path.is_file() and path.suffix in {".yaml", ".json", ".sh", ".md"}:
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            for word in FORBIDDEN_TEXT:
                if word.lower() in text:
                    raise SystemExit(f"{path} mentions {word!r}")
    run(
        str(python),
        "-c",
        "import bobd.service, bob_tasks.server, bob_mail.server, bob_knowledge.server, bob_voice.cli",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "app" / "runtime-stage"))
    parser.add_argument("--python", help="use this Python 3.12 instead of fetching one with uv")
    parser.add_argument("--no-voice-model", action="store_true", help="do not bundle the Whisper model")
    args = parser.parse_args()
    stage = Path(args.out).resolve()
    if stage.name != "runtime-stage":
        raise SystemExit("refusing to clear a folder that is not called runtime-stage")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as scratch:
        scratch_dir = Path(scratch)
        if args.python:
            # A copy of a venv or install the caller gave; used for testing the steps off a Mac.
            interpreter = Path(args.python)
            target = stage / "python"
            run("uv", "venv", "--python", str(interpreter), str(target))
            python = target / "bin" / "python3"
        else:
            run("uv", "python", "install", "3.12", "--install-dir", str(scratch_dir / "py"))
            found = sorted((scratch_dir / "py").glob("cpython-3.12*"))
            if not found:
                raise SystemExit("uv did not install Python 3.12")
            shutil.copytree(found[0], stage / "python", symlinks=True)
            python = stage / "python" / "bin" / "python3"

        wheels = scratch_dir / "wheels"
        for dist, _package in SHIPPED:
            run("uv", "build", "--wheel", "--package", dist, "--out-dir", str(wheels), cwd=ROOT)
        # Third-party packages exactly as locked, then Bob's own wheels on top.
        locked = scratch_dir / "requirements.txt"
        locked.write_text(
            run("uv", "export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-workspace",
                cwd=ROOT),
            encoding="utf-8",
        )  # fmt: skip
        install = ["uv", "pip", "install", "--python", str(python), "--no-deps"]
        if not args.python:
            install.append("--break-system-packages")
        run(*install, "-r", str(locked))
        run(*install, *[str(w) for w in sorted(wheels.glob("*.whl"))])

        site = site_packages(python)
        compiled = sum(to_bytecode(python, site / package) for _dist, package in SHIPPED)
        print(f"compiled {compiled} source files to bytecode and removed the source")

    shutil.copy2(HERE / "bobd.sh", stage / "bobd.sh")
    shutil.copy2(HERE / "config.template.yaml", stage / "config.template.yaml")
    shutil.copy2(HERE / "ollama.json", stage / "ollama.json")
    (stage / "models.json").write_text(
        json.dumps(models_json(HERE / "models.txt"), separators=(",", ":")), encoding="utf-8"
    )
    if not args.no_voice_model:
        bundle_voice_model(stage)
    check(stage, python, site)
    size = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file()) / 1e6
    print(f"staged {stage} ({size:.0f} MB)")


if __name__ == "__main__":
    if not re.match(r"3\.1[2-9]", ".".join(map(str, sys.version_info[:2]))):
        raise SystemExit("run this with Python 3.12 or newer")
    main()
