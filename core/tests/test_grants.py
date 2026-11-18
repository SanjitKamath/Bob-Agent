"""Folders Bob may work in: only ones you wrote in the chat, only after you approve, only there."""

from __future__ import annotations

import asyncio

import pytest
from bobd.broker import Caller, ToolDenied
from bobd.grants import named_paths


def caller(said: str, *, session: str = "s1", interactive: bool = True, kind: str = "model"):
    return Caller(
        kind=kind, name="notes", plugin="notes", session=session, interactive=interactive, said=said
    )


async def answer(bob, approved: bool) -> dict:
    for _ in range(200):
        pending = bob.confirmations.pending()
        if pending:
            card = pending[0]
            assert bob.confirmations.resolve(card.id, approved)
            return card.arguments
        await asyncio.sleep(0.01)
    raise AssertionError("no confirm card appeared")


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "work folder"
    folder.mkdir()
    (folder / "a.txt").write_text("hello")
    return folder


# -- reading a path out of what you wrote ---------------------------------------------------


def test_paths_are_read_from_your_words(tmp_path):
    said = f"please tidy {tmp_path}/a/b. Also '{tmp_path}/with space' and and/or https://x.com/p ~"
    found = named_paths(said)
    assert found == [tmp_path / "with space", tmp_path / "a" / "b"]  # quoted first, no punctuation
    assert named_paths("/ and ~ and a/b") == []


# -- the grant flow -------------------------------------------------------------------------


async def test_a_named_folder_asks_once_then_covers_everything_inside(bob, work):
    me = caller(f"in '{work}' please read a.txt")
    task = asyncio.create_task(
        bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt")}, me)
    )
    card = await answer(bob, True)
    assert card["folder"] == str(work) and card["lasts"] == "this chat only"
    assert (await task) == {"text": "hello", "session": "s1"}

    (work / "sub").mkdir()
    # No second card: the folder and its children are covered, for reads and for changes.
    await bob.broker.call(
        "scribe", "write_text", {"path": str(work / "sub" / "b.txt"), "text": "x"}, me
    )
    assert (work / "sub" / "b.txt").read_text() == "x"
    assert bob.confirmations.pending() == []
    assert [g.root for g in bob.grants.list("s1")] == [work]


async def test_nothing_is_allowed_outside_the_folder_you_named(bob, work, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.txt").write_text("no")
    me = caller(f"look in '{work}'")
    with pytest.raises(ToolDenied, match="not in a folder the user named"):
        await bob.broker.call("scribe", "read_text", {"path": str(other / "x.txt")}, me)
    with pytest.raises(ToolDenied, match="not in a folder the user named"):
        await bob.broker.call(
            "scribe", "read_text", {"path": str(work / ".." / "other" / "x.txt")}, me
        )
    assert bob.confirmations.pending() == [] and bob.grants.list() == []


async def test_a_model_cannot_name_a_folder_itself(bob, work):
    me = caller("tidy my files")  # no path in anything you wrote
    with pytest.raises(ToolDenied, match="not in a folder the user named"):
        await bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt")}, me)


async def test_declining_means_no_and_nothing_is_written(bob, work):
    me = caller(f"'{work}'")
    target = work / "new.txt"
    task = asyncio.create_task(
        bob.broker.call("scribe", "write_text", {"path": str(target), "text": "x"}, me)
    )
    await answer(bob, False)
    with pytest.raises(ToolDenied, match="declined"):
        await task
    assert not target.exists() and bob.grants.list() == []


async def test_a_grant_is_for_one_chat_only(bob, work):
    task = asyncio.create_task(
        bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt")}, caller(f"'{work}'"))
    )
    await answer(bob, True)
    await task
    elsewhere = caller(f"'{work}'", session="s2")
    task = asyncio.create_task(
        bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt")}, elsewhere)
    )
    await answer(bob, True)  # the other chat is asked again
    assert (await task)["session"] == "s2"


async def test_revoking_ends_the_access(bob, work):
    me = caller(f"'{work}'")
    task = asyncio.create_task(
        bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt")}, me)
    )
    await answer(bob, True)
    await task
    assert bob.grants.revoke_session("s1") == 1
    task = asyncio.create_task(
        bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt")}, me)
    )
    await answer(bob, False)
    with pytest.raises(ToolDenied):
        await task


async def test_naming_a_single_file_grants_only_that_file(bob, work):
    me = caller(f"fix '{work / 'a.txt'}'")
    task = asyncio.create_task(
        bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt")}, me)
    )
    card = await answer(bob, True)
    assert "this one file" in card["allows"]
    await task
    with pytest.raises(ToolDenied, match="not in a folder the user named"):
        await bob.broker.call("scribe", "read_text", {"path": str(work / "b.txt")}, me)


async def test_the_chat_id_comes_from_the_core_not_the_model(bob, work):
    me = caller(f"'{work}'")
    task = asyncio.create_task(
        bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt"), "session": "evil"}, me)
    )
    await answer(bob, True)
    assert (await task)["session"] == "s1"


async def test_it_needs_someone_to_ask(bob, work):
    quiet = caller(f"'{work}'", interactive=False)
    with pytest.raises(ToolDenied, match="needs the user's approval"):
        await bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt")}, quiet)
    routine = caller(f"'{work}'", kind="routine")
    with pytest.raises(ToolDenied, match="routine cannot"):
        await bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt")}, routine)
    plugin = caller(f"'{work}'", kind="plugin")
    with pytest.raises(ToolDenied):
        await bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt")}, plugin)


# -- what no approval can unlock ------------------------------------------------------------


async def test_credentials_are_never_touched(bob, work):
    (work / ".env").write_text("TOKEN=1")
    me = caller(f"'{work}'")
    for tool, args in (("read_text", {}), ("write_text", {"text": "x"})):
        with pytest.raises(ToolDenied, match="credentials"):
            await bob.broker.call("scribe", tool, {"path": str(work / ".env"), **args}, me)
    assert (work / ".env").read_text() == "TOKEN=1" and bob.confirmations.pending() == []


async def test_a_git_repository_can_be_read_but_never_changed(bob, work):
    (work / ".git").mkdir()
    (work / "code.py").write_text("print(1)")
    me = caller(f"'{work}'")
    task = asyncio.create_task(
        bob.broker.call("scribe", "read_text", {"path": str(work / "code.py")}, me)
    )
    await answer(bob, True)
    assert (await task)["text"] == "print(1)"
    with pytest.raises(ToolDenied, match="git repository"):
        await bob.broker.call(
            "scribe", "write_text", {"path": str(work / "code.py"), "text": "x"}, me
        )
    with pytest.raises(ToolDenied, match="credentials"):
        await bob.broker.call("scribe", "read_text", {"path": str(work / ".git" / "config")}, me)
    assert (work / "code.py").read_text() == "print(1)"


async def test_your_home_folder_and_system_folders_cannot_be_granted(bob, tmp_path):
    from pathlib import Path

    home = Path.home()
    for said, target in ((f"{home}", home / "x.txt"), ("/etc", Path("/etc/hosts"))):
        with pytest.raises(ToolDenied):
            await bob.broker.call(
                "scribe", "write_text", {"path": str(target), "text": "x"}, caller(said)
            )
    with pytest.raises(ToolDenied, match="hidden"):
        await bob.broker.call(
            "scribe",
            "write_text",
            {"path": str(home / ".zshrc"), "text": "x"},
            caller(f"'{home}/.zshrc'"),
        )


async def test_bobs_own_folders_cannot_be_changed(bob):
    mine = bob.config.data_dir / "notes.txt"
    with pytest.raises(ToolDenied, match="Bob's own"):
        await bob.broker.call(
            "scribe",
            "write_text",
            {"path": str(mine), "text": "x"},
            caller(f"'{bob.config.data_dir}'"),
        )


async def test_every_decision_is_audited(bob, work):
    me = caller(f"'{work}'")
    task = asyncio.create_task(
        bob.broker.call("scribe", "read_text", {"path": str(work / "a.txt")}, me)
    )
    await answer(bob, True)
    await task
    assert [e.outcome for e in bob.audit.entries(kind="grant")] == ["ok"]
    assert any(e.tier == "read_granted" for e in bob.audit.entries(kind="tool"))


# -- speech is never where a folder is named ------------------------------------------------


async def test_a_spoken_request_cannot_name_a_folder(bob, model, work):
    chat = bob.memory.ensure_conversation(None)
    bob.memory.add_turn(chat, "user", f"tidy '{work}'", source="voice")
    bob.memory.add_turn(chat, "assistant", "ok", "notes")
    bob.memory.add_turn(chat, "user", "earlier typed words")
    orchestrator = bob.orchestrator
    from bobd import agent

    agent._VOICE_TURN.set(True)
    assert str(work) not in orchestrator._user_words(chat, f"and also '{work}'")
    assert "earlier typed words" in orchestrator._user_words(chat, "x")
    agent._VOICE_TURN.set(False)
    assert str(work) in orchestrator._user_words(chat, f"and also '{work}'")


async def test_turns_remember_whether_they_were_spoken(bob):
    reply = await bob.orchestrator.handle("hello there", voice=True)
    again = await bob.orchestrator.handle("hello again", conversation_id=reply.conversation_id)
    turns = [t for t in bob.memory.recent_turns(reply.conversation_id) if t.role == "user"]
    assert [(t.content, t.source) for t in turns] == [
        ("hello there", "voice"),
        ("hello again", "typed"),
    ]
    assert again.conversation_id == reply.conversation_id
