from __future__ import annotations

import stat
from pathlib import Path

import pytest
from bob_architect import design
from bob_sdk.output import OutputFolder
from bob_sdk.runtime import ToolError, check_declared


@pytest.fixture
def folder(tmp_path):
    return OutputFolder(tmp_path / "out", protected=[])


def test_adrs_are_numbered_and_never_overwritten(folder):
    first = design.new_adr(folder, "Use  LanceDB for vectors", decision="We use LanceDB.")
    second = design.new_adr(folder, "Use LanceDB for vectors")
    assert first["number"] == 1 and first["path"].endswith("adr/0001-use-lancedb-for-vectors.md")
    assert second["number"] == 2 and second["path"].endswith("adr/0002-use-lancedb-for-vectors.md")
    text = Path(first["path"]).read_text()
    assert text.startswith("# 1. Use LanceDB for vectors\n") and "We use LanceDB." in text
    assert "TODO: what forces are at play?" in text
    with pytest.raises(ToolError, match="needs a title"):
        design.new_adr(folder, "   ")


def test_c4_skeleton_is_valid_mermaid_source(folder):
    result = design.c4_skeleton(folder, "Bob", "bobd, Knowledge store, 3rd party", "Ada")
    source = result["source"]
    assert source.startswith("C4Container\n    title Container diagram for Bob\n")
    assert 'Person(Ada, "Ada")' in source
    assert 'Container(Knowledge_store, "Knowledge store"' in source
    assert 'Container(n_3rd_party, "3rd party"' in source  # identifiers cannot start with a digit
    assert "Rel(Ada, bobd" in source and "Rel(bobd, Knowledge_store" in source
    assert result["path"].endswith("containers.mmd") and "/c4/Bob_" in result["path"]
    default = design.c4_skeleton(folder, 'Pay"ments')
    assert "Pay'ments API" in default["source"]
    with pytest.raises(ToolError):
        design.c4_skeleton(folder, " ")


def test_render_needs_the_cli_and_reports_it(folder, monkeypatch):
    monkeypatch.setattr(design.shutil, "which", lambda name: None)
    with pytest.raises(ToolError, match="mmdc is not installed"):
        design.render_diagram(folder, "graph TD; A-->B", "mermaid", "flow")
    with pytest.raises(ToolError, match="empty"):
        design.render_diagram(folder, "  ", "d2", "flow")
    with pytest.raises(ToolError, match="kind must be"):
        design.render_diagram(folder, "x", "dot", "flow")  # type: ignore[arg-type]


def test_render_writes_source_and_svg_as_new_files(folder, tmp_path, monkeypatch):
    fake = tmp_path / "d2"
    fake.write_text('#!/bin/sh\nprintf "<svg>%s</svg>" "$(cat "$1")" > "$2"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(design.shutil, "which", lambda name: str(fake))
    result = design.render_diagram(folder, "a -> b", "d2", "My flow!")
    assert Path(result["svg"]).read_text() == "<svg>a -> b</svg>"
    assert Path(result["source"]).read_text() == "a -> b\n"
    assert "/diagrams/My_flow_" in result["svg"]

    failing = tmp_path / "bad"
    failing.write_text("#!/bin/sh\necho 'syntax error on line 1' >&2\nexit 1\n")
    failing.chmod(failing.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(design.shutil, "which", lambda name: str(failing))
    with pytest.raises(ToolError, match="syntax error on line 1"):
        design.render_diagram(folder, "a -> b", "d2", "flow")


def test_server_declares_exactly_its_tools():
    from bob_architect import server

    check_declared(server.server, server.MANIFEST)
