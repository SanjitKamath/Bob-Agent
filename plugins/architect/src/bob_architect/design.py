"""Diagrams, ADRs and C4 skeletons, written as new files in the output folder."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Literal

from bob_sdk.output import OutputFolder, OutputFolderError, safe_name
from bob_sdk.runtime import ToolError

DiagramKind = Literal["mermaid", "d2"]
MAX_SOURCE_CHARS = 60_000
_ADR_FILE = re.compile(r"^(\d{4})-.*\.md$")
_ID = re.compile(r"[^A-Za-z0-9]+")

_RENDERERS: dict[str, tuple[str, str, str]] = {
    # kind -> (binary, source suffix, install hint)
    "mermaid": ("mmdc", ".mmd", "npm install -g @mermaid-js/mermaid-cli"),
    "d2": ("d2", ".d2", "brew install d2"),
}


def _guarded(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except OutputFolderError as exc:
        raise ToolError(str(exc)) from exc


def render_diagram(
    folder: OutputFolder, source: str, kind: DiagramKind, name: str
) -> dict[str, Any]:
    """Render diagram source to SVG. Writes the source and the SVG as new files."""
    if kind not in _RENDERERS:
        raise ToolError("kind must be 'mermaid' or 'd2'")
    source = source.strip()
    if not source:
        raise ToolError("the diagram source is empty")
    if len(source) > MAX_SOURCE_CHARS:
        raise ToolError("the diagram source is too long")
    binary_name, suffix, hint = _RENDERERS[kind]
    binary = shutil.which(binary_name)
    if binary is None:
        raise ToolError(f"{binary_name} is not installed ({hint})")
    stem = _guarded(safe_name, name)
    with tempfile.TemporaryDirectory(prefix="bob-diagram-") as scratch:
        source_file = Path(scratch) / f"{stem}{suffix}"
        svg_file = Path(scratch) / f"{stem}.svg"
        source_file.write_text(source + "\n", encoding="utf-8")
        command = (
            [binary, "-i", str(source_file), "-o", str(svg_file), "--quiet"]
            if kind == "mermaid"
            else [binary, str(source_file), str(svg_file)]
        )
        try:
            done = subprocess.run(  # noqa: S603 - fixed binary; arguments are our own temp paths
                command, capture_output=True, text=True, timeout=120, check=False, cwd=scratch
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"{binary_name} timed out") from exc
        if done.returncode != 0 or not svg_file.is_file():
            detail = (done.stderr or done.stdout).strip()[-1500:]
            raise ToolError(f"the diagram did not render: {detail or 'no output'}")
        run_dir = _guarded(folder.new_run_dir, stem, subfolder="diagrams")
        written_source = _guarded(folder.write_new, run_dir / source_file.name, source + "\n")
        written_svg = _guarded(folder.write_new, run_dir / svg_file.name, svg_file.read_bytes())
    return {"svg": str(written_svg), "source": str(written_source), "kind": kind}


def new_adr(
    folder: OutputFolder, title: str, context: str = "", decision: str = "", consequences: str = ""
) -> dict[str, Any]:
    """Create the next numbered Architecture Decision Record."""
    title = " ".join(title.split())
    if not title:
        raise ToolError("an ADR needs a title")
    adr_dir = _guarded(folder.resolve_inside, "adr")
    adr_dir.mkdir(parents=True, exist_ok=True)
    numbers = [
        int(match.group(1)) for item in adr_dir.iterdir() if (match := _ADR_FILE.match(item.name))
    ]
    number = max(numbers, default=0) + 1
    slug = _ID.sub("-", title.lower()).strip("-")[:60] or "decision"
    body = (
        f"# {number}. {title}\n\n"
        f"Date: {date.today().isoformat()}\n\n"
        "## Status\n\nProposed\n\n"
        f"## Context\n\n{context.strip() or 'TODO: what forces are at play?'}\n\n"
        f"## Decision\n\n{decision.strip() or 'TODO: what did we decide?'}\n\n"
        f"## Consequences\n\n{consequences.strip() or 'TODO: what becomes easier or harder?'}\n"
    )
    path = _guarded(folder.write_new, adr_dir / f"{number:04d}-{slug}.md", body)
    return {"path": str(path), "number": number, "title": title}


def _ident(label: str) -> str:
    ident = _ID.sub("_", label).strip("_")
    return ident if ident and not ident[0].isdigit() else f"n_{ident}"


def c4_source(system: str, containers: list[str], users: list[str]) -> str:
    """Mermaid C4 container diagram skeleton."""
    lines = ["C4Container", f"    title Container diagram for {system}", ""]
    for user in users:
        lines.append(f'    Person({_ident(user)}, "{user}")')
    lines += ["", f'    System_Boundary({_ident(system)}, "{system}") {{']
    for container in containers:
        lines.append(
            f'        Container({_ident(container)}, "{container}", "TODO: technology", "TODO: responsibility")'
        )
    lines += ["    }", ""]
    if users and containers:
        lines.append(f'    Rel({_ident(users[0])}, {_ident(containers[0])}, "Uses")')
    for upstream, downstream in zip(containers, containers[1:], strict=False):
        lines.append(f'    Rel({_ident(upstream)}, {_ident(downstream)}, "TODO: calls")')
    return "\n".join(lines).rstrip() + "\n"


def _names(value: str) -> list[str]:
    seen: list[str] = []
    for item in value.split(","):
        name = " ".join(item.replace('"', "'").split())
        if name and name not in seen:
            seen.append(name)
    return seen


def c4_skeleton(
    folder: OutputFolder, system: str, containers: str = "", users: str = "User"
) -> dict[str, Any]:
    """Write a C4 container diagram skeleton (Mermaid) to refine and render."""
    system = " ".join(system.replace('"', "'").split())
    if not system:
        raise ToolError("a system name is required")
    container_names = _names(containers) or [f"{system} API", f"{system} Database"]
    source = c4_source(system, container_names, _names(users))
    run_dir = _guarded(folder.new_run_dir, system, subfolder="c4")
    path = _guarded(folder.write_new, run_dir / "containers.mmd", source)
    return {"path": str(path), "kind": "mermaid", "source": source}
