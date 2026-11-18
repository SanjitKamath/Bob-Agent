"""MCP server for the Architect plugin. Tools only create new files in its output folder."""

from __future__ import annotations

from typing import Any, Literal

from bob_sdk.output import OutputFolder
from bob_sdk.runtime import PluginContext, create_server, manifest_for, run

from . import design

MANIFEST = manifest_for(__file__)
server = create_server(MANIFEST)
_ctx: PluginContext | None = None


def folder() -> OutputFolder:
    global _ctx
    if _ctx is None:
        _ctx = PluginContext.load(MANIFEST)
    return _ctx.output_folder("output_dir")


@server.tool()
def render_diagram(
    source: str, kind: Literal["mermaid", "d2"] = "mermaid", name: str = "diagram"
) -> dict[str, Any]:
    """Render Mermaid or D2 diagram source to an SVG file."""
    return design.render_diagram(folder(), source, kind, name)


@server.tool()
def new_adr(
    title: str, context: str = "", decision: str = "", consequences: str = ""
) -> dict[str, Any]:
    """Create the next numbered Architecture Decision Record as a Markdown file."""
    return design.new_adr(folder(), title, context, decision, consequences)


@server.tool()
def c4_skeleton(system: str, containers: str = "", users: str = "User") -> dict[str, Any]:
    """Write a C4 container diagram skeleton. containers and users are comma-separated names."""
    return design.c4_skeleton(folder(), system, containers, users)


def main() -> None:
    run(server, MANIFEST)


if __name__ == "__main__":
    main()
