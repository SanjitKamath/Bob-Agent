from __future__ import annotations

import asyncio
import subprocess

import pytest
from bobd.broker import USER, Caller, ToolDenied, ToolFailed
from bobd.plugins import ConsentRequired, PluginError

MODEL = Caller(kind="model", name="notes", plugin="notes", session="s1", interactive=True)
QUIET_MODEL = Caller(kind="model", name="notes", plugin="notes")


async def test_discovery_rejects_a_tier_outside_the_vocabulary(bob):
    assert "badtier" not in bob.plugins.plugins
    reason = next(r for location, r in bob.plugins.rejected.items() if "badtier" in location)
    assert "tools.push_code" in reason


async def test_first_run_starts_listed_and_default_plugins(bob):
    assert bob.plugins.get("notes").status == "running"
    assert bob.plugins.get("knowledge").status == "running"  # default_enabled
    assert bob.plugins.get("rogue").status == "stopped"


async def test_a_server_offering_an_undeclared_tool_is_refused(bob):
    with pytest.raises(PluginError, match="undeclared tool.*run_shell"):
        await bob.plugins.enable("rogue", acknowledged=True)
    assert bob.plugins.get("rogue").status == "error"
    with pytest.raises(ToolDenied):
        await bob.call_tool("rogue", "run_shell", {"command": "id"})


async def test_enabling_asks_first_then_disable_removes_tools_at_once(bob):
    await bob.plugins.disable("notes")
    assert bob.plugins.get("notes").status == "stopped"
    with pytest.raises(ToolDenied, match="switched off"):
        await bob.call_tool("notes", "list_notes")
    assert [s.id for s in bob.orchestrator.specialists()] == []

    # notes was acknowledged at first run; a never-seen plugin needs a yes.
    with pytest.raises(ConsentRequired) as consent:
        await bob.plugins.enable("rogue")
    assert consent.value.summary["tools"][0]["name"] == "hello"

    await bob.plugins.enable("notes")
    assert (await bob.call_tool("notes", "list_notes"))["notes"] == []


async def test_data_and_settings_survive_a_disable(bob):
    await bob.call_tool("notes", "add", {"text": "keep me"})
    await bob.plugins.update_settings("notes", {"greeting": "hi"})
    await bob.plugins.disable("notes")
    await bob.plugins.enable("notes")
    listing = await bob.call_tool("notes", "list_notes")
    assert listing == {"notes": ["keep me"], "greeting": "hi"}


async def test_settings_are_validated(bob, tmp_path):
    with pytest.raises(PluginError, match="no setting"):
        await bob.plugins.update_settings("notes", {"nope": 1})
    with pytest.raises(PluginError, match="must be a string"):
        await bob.plugins.update_settings("notes", {"greeting": 5})
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(PluginError, match="git repository"):
        await bob.plugins.update_settings("notes", {"output_dir": str(repo / "out")})
    with pytest.raises(PluginError, match="protected folder"):
        await bob.plugins.update_settings("notes", {"output_dir": str(tmp_path / "dev" / "out")})
    with pytest.raises(PluginError, match="protected folder"):
        await bob.plugins.update_settings(
            "notes", {"output_dir": str(tmp_path / "knowledge_base" / "out")}
        )


async def test_output_tool_never_overwrites(bob, tmp_path):
    result = await bob.call_tool("notes", "save_file", {"name": "a.txt", "text": "one"})
    assert result["path"].startswith(str((tmp_path / "output" / "notes").resolve()))
    with pytest.raises(ToolFailed, match="already exists"):
        await bob.call_tool("notes", "save_file", {"name": "a.txt", "text": "two"})
    with pytest.raises(ToolFailed, match="outside the output folder"):
        await bob.call_tool("notes", "save_file", {"name": "../../escape.txt", "text": "x"})


async def test_admin_tools_are_for_you_only(bob):
    with pytest.raises(ToolDenied, match="only be run by you"):
        await bob.broker.call("notes", "wipe", {}, QUIET_MODEL)
    assert (await bob.broker.call("notes", "wipe", {}, USER)) == {"ok": True}


async def test_a_switched_off_tool_cannot_be_called_by_anyone(bob):
    await bob.plugins.set_tool_enabled("notes", "add", False)
    for caller in (USER, QUIET_MODEL):
        with pytest.raises(ToolDenied, match="switched off in Settings"):
            await bob.broker.call("notes", "add", {"text": "x"}, caller)
    await bob.plugins.set_tool_enabled("notes", "add", True)
    assert (await bob.broker.call("notes", "add", {"text": "x"}, USER))["ok"]


async def test_external_tools_wait_for_your_approval(bob):
    async def decide(approved: bool):
        for _ in range(100):
            pending = bob.confirmations.pending()
            if pending:
                assert pending[0].tool == "send" and pending[0].session == "s1"
                assert bob.confirmations.resolve(pending[0].id, approved)
                return
            await asyncio.sleep(0.01)
        raise AssertionError("no confirm card appeared")

    call = asyncio.create_task(bob.broker.call("notes", "send", {"to": "a@b.c"}, MODEL))
    await decide(True)
    assert await call == {"sent_to": "a@b.c"}

    call = asyncio.create_task(bob.broker.call("notes", "send", {"to": "a@b.c"}, MODEL))
    await decide(False)
    with pytest.raises(ToolDenied, match="declined"):
        await call

    outcomes = [e.outcome for e in bob.audit.entries(kind="tool") if e.action == "notes.send"]
    assert outcomes == ["declined", "ok"]  # newest first
    assert all(
        e.tier == "external" for e in bob.audit.entries(kind="tool") if e.action == "notes.send"
    )


async def test_external_tools_are_denied_when_nobody_can_be_asked(bob):
    with pytest.raises(ToolDenied, match="needs your approval"):
        await bob.broker.call("notes", "send", {"to": "x"}, QUIET_MODEL)
    routine = Caller(kind="routine", name="r", plugin="notes")
    with pytest.raises(ToolDenied, match="routine cannot"):
        await bob.broker.call("notes", "send", {"to": "x"}, routine)
    assert bob.confirmations.pending() == []


async def test_unanswered_confirm_card_times_out_as_no(bob):
    bob.confirmations._timeout = 0.05
    with pytest.raises(ToolDenied, match="declined"):
        await bob.broker.call("notes", "send", {"to": "x"}, MODEL)


async def test_knowledge_is_scoped_by_the_manifest_uses_list(bob):
    allowed = await bob.broker.call(
        "knowledge", "search", {"collection": "notes", "query": "q"}, MODEL
    )
    assert allowed["found"]
    with pytest.raises(ToolDenied, match="may not read the collection 'secret'"):
        await bob.broker.call("knowledge", "search", {"collection": "secret", "query": "q"}, MODEL)
    listing = await bob.broker.call("knowledge", "list_collections", {}, MODEL)
    assert listing == {"collections": [{"id": "notes"}]}
    everything = await bob.broker.call("knowledge", "list_collections", {}, USER)
    assert len(everything["collections"]) == 2


async def test_plugins_reach_each_other_only_through_the_core(bob):
    # The notes server calls knowledge.search through the core with its own token.
    assert bob.config.server.port  # the test API below stands in for the HTTP layer
    plugin_caller = Caller(kind="plugin", name="notes", plugin="notes")
    ok = await bob.broker.call(
        "knowledge", "search", {"collection": "notes", "query": "q"}, plugin_caller
    )
    assert ok["found"]
    with pytest.raises(ToolDenied, match="may not read"):
        await bob.broker.call(
            "knowledge", "search", {"collection": "secret", "query": "q"}, plugin_caller
        )
    other = Caller(kind="plugin", name="knowledge", plugin="knowledge")
    with pytest.raises(ToolDenied, match="may not call notes.add"):
        await bob.broker.call("notes", "add", {"text": "x"}, other)


async def test_audit_log_is_append_only(bob):
    bob.audit.record(
        actor="user", kind="tool", action="x", outcome="ok", arguments={"password": "p"}
    )
    entry = bob.audit.entries(limit=1)[0]
    assert entry.arguments == {"password": "***"}
    import sqlite3

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        bob.db.execute("DELETE FROM audit_log")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        bob.db.execute("UPDATE audit_log SET outcome='tampered'")
