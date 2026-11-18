from __future__ import annotations

import pytest
from bob_files.finder import Finder
from bob_sdk.paths import ReadGuard, is_secret
from bob_sdk.runtime import ToolError


@pytest.fixture
def tree(tmp_path):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "notes.md").write_text("alpha\nBeta line\ngamma\n")
    (docs / "sub" / "code.py").write_text("def beta():\n    return 1\n")
    (docs / ".env").write_text("SECRET=beta\n")
    (docs / "secrets").mkdir()
    (docs / "secrets" / "key.txt").write_text("beta\n")
    (docs / "blob.bin").write_bytes(b"\x00\x01beta")
    (tmp_path / "outside.txt").write_text("beta\n")
    return docs


@pytest.fixture
def finder(tree):
    return Finder(ReadGuard(str(tree)))


def test_secrets_are_recognised(tmp_path):
    for name in (".env", ".env.local", "id_rsa", "server.pem", "secrets/x.txt", ".ssh/config"):
        assert is_secret(tmp_path / name), name
    assert not is_secret(tmp_path / "environment.md")


def test_grep_finds_text_and_skips_secrets_and_binaries(finder, tree):
    result = finder.grep("beta", str(tree))
    assert [(m["path"].rsplit("/", 1)[-1], m["line"]) for m in result["matches"]] == [
        ("notes.md", 2),
        ("code.py", 1),
    ]
    assert finder.grep("beta", str(tree), ignore_case=False)["matches"][0]["path"].endswith(
        "code.py"
    )
    assert finder.grep("beta", str(tree), limit=1)["truncated"] is True
    with pytest.raises(ToolError, match="invalid regular expression"):
        finder.grep("(", str(tree))


def test_reads_are_confined_to_the_allowed_folders(finder, tree, tmp_path):
    with pytest.raises(ToolError, match="outside the folders"):
        finder.read_file(str(tmp_path / "outside.txt"))
    with pytest.raises(ToolError, match="outside the folders"):
        finder.read_file(str(tree / ".." / "outside.txt"))
    with pytest.raises(ToolError, match="never read"):
        finder.read_file(str(tree / ".env"))
    with pytest.raises(ToolError, match="never read"):
        finder.grep("x", str(tree / "secrets"))
    link = tree / "link.txt"
    link.symlink_to(tmp_path / "outside.txt")
    with pytest.raises(ToolError, match="outside the folders"):
        finder.read_file(str(link))


def test_read_file_ranges_and_binary_refusal(finder, tree):
    whole = finder.read_file(str(tree / "notes.md"))
    assert whole["text"] == "alpha\nBeta line\ngamma" and whole["total_lines"] == 3
    part = finder.read_file(str(tree / "notes.md"), start_line=2, max_lines=1)
    assert (part["text"], part["start_line"], part["end_line"], part["truncated"]) == (
        "Beta line", 2, 2, True,
    )  # fmt: skip
    with pytest.raises(ToolError, match="not a text file"):
        finder.read_file(str(tree / "blob.bin"))
    with pytest.raises(ToolError, match="does not exist"):
        finder.read_file(str(tree / "missing.md"))


def test_list_dir_hides_secrets(finder, tree):
    names = [entry["name"] for entry in finder.list_dir(str(tree))["entries"]]
    assert names == ["blob.bin", "notes.md", "sub"]
    with pytest.raises(ToolError, match="not a folder"):
        finder.list_dir(str(tree / "notes.md"))


def test_server_declares_exactly_its_tools():
    from bob_files import server
    from bob_sdk.runtime import check_declared

    check_declared(server.server, server.MANIFEST)
