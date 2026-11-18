from __future__ import annotations

import pytest
from bob_editor.journal import Journal
from bob_editor.ops import Editor
from bob_sdk.runtime import ToolError

S = "chat-1"


@pytest.fixture
def folder(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return work


@pytest.fixture
def editor(tmp_path):
    journal = Journal(tmp_path / "journal")
    yield Editor(journal, protected=[])
    journal.close()


def test_a_new_file_is_created_and_undo_removes_it(editor, folder):
    target = folder / "a.txt"
    assert editor.write_file(str(target), "hi", S)["created"] is True
    assert target.read_text() == "hi"
    assert editor.undo(S)["undone"][0]["op"] == "write"
    assert not target.exists()


def test_overwriting_keeps_the_original_for_undo(editor, folder):
    target = folder / "a.txt"
    target.write_text("original")
    editor.write_file(str(target), "new", S)
    editor.edit_file(str(target), "new", "newer", S)
    assert target.read_text() == "newer"
    editor.undo(S, steps=2)
    assert target.read_text() == "original"


def test_undo_will_not_throw_away_your_own_later_edits(editor, folder):
    target = folder / "a.txt"
    target.write_text("original")
    editor.edit_file(str(target), "original", "bob's", S)
    target.write_text("what you typed afterwards")
    result = editor.undo(S)
    assert result["undone"] == [] and "changed since" in result["stopped_because"]
    assert target.read_text() == "what you typed afterwards"


def test_edit_needs_exact_unique_text(editor, folder):
    target = folder / "a.txt"
    target.write_text("x and x")
    with pytest.raises(ToolError, match="not in the file"):
        editor.edit_file(str(target), "y", "z", S)
    with pytest.raises(ToolError, match="2 times"):
        editor.edit_file(str(target), "x", "z", S)
    assert editor.edit_file(str(target), "x", "z", S, replace_all=True)["replaced"] == 2
    assert target.read_text() == "z and z"


def test_delete_keeps_a_whole_folder_and_undo_restores_it(editor, folder):
    (folder / "d" / "inner").mkdir(parents=True)
    (folder / "d" / "inner" / "f.txt").write_text("keep me")
    assert editor.delete(str(folder / "d"), S)["files"] == 1
    assert not (folder / "d").exists()
    editor.undo(S)
    assert (folder / "d" / "inner" / "f.txt").read_text() == "keep me"


def test_move_and_make_dir_can_be_undone(editor, folder):
    (folder / "a.txt").write_text("a")
    editor.make_dir(str(folder / "sub"), S)
    editor.move(str(folder / "a.txt"), str(folder / "sub" / "b.txt"), S)
    assert (folder / "sub" / "b.txt").exists()
    editor.undo(S, steps=2)
    assert (folder / "a.txt").read_text() == "a" and not (folder / "sub").exists()


def test_a_move_never_overwrites(editor, folder):
    (folder / "a.txt").write_text("a")
    (folder / "b.txt").write_text("b")
    with pytest.raises(ToolError, match="already exists"):
        editor.move(str(folder / "a.txt"), str(folder / "b.txt"), S)


def test_undo_goes_newest_first_and_each_change_only_once(editor, folder):
    editor.write_file(str(folder / "1.txt"), "1", S)
    editor.write_file(str(folder / "2.txt"), "2", S)
    first = editor.undo(S)["undone"]
    assert first[0]["path"].endswith("2.txt") and (folder / "1.txt").exists()
    editor.undo(S)
    assert editor.undo(S)["stopped_because"].startswith("there is nothing")
    assert [c["undone"] for c in editor.history(S)["changes"]] == [True, True]


def test_chats_do_not_undo_each_others_changes(editor, folder):
    editor.write_file(str(folder / "1.txt"), "1", "chat-a")
    assert editor.undo("chat-b")["undone"] == []
    assert (folder / "1.txt").exists()


def test_changes_need_a_chat(editor, folder):
    with pytest.raises(ToolError, match="inside a chat"):
        editor.write_file(str(folder / "a.txt"), "x", "")


def test_credentials_and_repositories_are_refused_here_too(editor, folder):
    (folder / ".env").write_text("T=1")
    with pytest.raises(ToolError, match="credentials"):
        editor.read_file(str(folder / ".env"))
    with pytest.raises(ToolError, match="credentials"):
        editor.write_file(str(folder / ".env"), "x", S)
    (folder / ".git").mkdir()
    (folder / "x.py").write_text("1")
    assert editor.read_file(str(folder / "x.py"))["text"] == "1"
    with pytest.raises(ToolError, match="git repository"):
        editor.edit_file(str(folder / "x.py"), "1", "2", S)
    with pytest.raises(ToolError, match="git repository"):
        editor.delete(str(folder / "x.py"), S)


def test_the_journal_cannot_be_edited_through_the_editor(tmp_path, editor):
    with pytest.raises(ToolError, match="protected"):
        editor.write_file(str(tmp_path / "journal" / "x.txt"), "x", S)


def test_listing_and_finding_hide_secrets_and_binaries(editor, folder):
    (folder / "notes.md").write_text("alpha\nTODO fix\n")
    (folder / "key.pem").write_text("-----BEGIN-----")
    (folder / ".env").write_text("TODO secret")
    (folder / "pic.bin").write_bytes(b"\0\1TODO")
    names = [e["name"] for e in editor.list_dir(str(folder))["entries"]]
    assert "notes.md" in names and ".env" not in names and "key.pem" not in names
    found = editor.find(str(folder), text="todo")["found"]
    assert [f["path"].rsplit("/", 1)[1] for f in found] == ["notes.md"]
    assert found[0]["matches"][0]["line"] == 2
    assert [
        f["path"].rsplit("/", 1)[1] for f in editor.find(str(folder), name="*.MD")["found"]
    ] == ["notes.md"]
    with pytest.raises(ToolError, match="not a text file"):
        editor.read_file(str(folder / "pic.bin"))
