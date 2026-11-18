from __future__ import annotations

from datetime import datetime

import pytest
from bobd.config import Config, ConfigError, load_config
from bobd.conformance import check_filesystem, check_tools, run_conformance
from bobd.scheduler import in_window


def test_in_window_handles_plain_and_overnight_ranges():
    morning = datetime(2026, 11, 13, 9, 0)
    assert in_window(None, morning)
    assert in_window(("08:00", "20:00"), morning)
    assert not in_window(("08:00", "20:00"), datetime(2026, 11, 13, 20, 0))
    assert not in_window(("08:00", "20:00"), datetime(2026, 11, 13, 7, 59))
    assert in_window(("22:00", "06:00"), datetime(2026, 11, 13, 23, 30))
    assert in_window(("22:00", "06:00"), datetime(2026, 11, 13, 5, 0))
    assert not in_window(("22:00", "06:00"), datetime(2026, 11, 13, 12, 0))


async def test_routines_come_from_running_plugins_and_follow_them(bob):
    ids = {routine.id for routine in bob.scheduler.routines()}
    assert ids == {"notes-tick", "notes-brief", "conformance", "evals"}
    assert bob.scheduler.next_run("notes-tick") is not None

    await bob.plugins.disable("notes")
    assert {routine.id for routine in bob.scheduler.routines()} == {"conformance", "evals"}
    assert bob.scheduler.next_run("notes-tick") is None  # removed with no restart

    await bob.plugins.enable("notes")
    assert bob.scheduler.next_run("notes-tick") is not None


async def test_a_tool_routine_runs_and_is_recorded(bob):
    result = await bob.scheduler.run("notes-tick")
    assert result["outcome"] == "ok" and result["result"]["count"] == 1
    assert (await bob.call_tool("notes", "list_notes"))["notes"] == ["tick"]
    run = bob.scheduler.history("notes-tick")[0]
    assert run["outcome"] == "ok" and run["finished_at"]
    added = next(e for e in bob.audit.entries(kind="tool") if e.action == "notes.add")
    assert added.actor == "routine:notes-tick"


async def test_a_prompt_routine_runs_the_specialist_and_makes_a_card(bob, model):
    model.steps = ["You have no notes."]
    result = await bob.scheduler.run("notes-brief")
    assert result["outcome"] == "ok"
    card = bob.cards()[0]
    assert (card["kind"], card["title"], card["body"]) == (
        "brief",
        "Notes brief",
        "You have no notes.",
    )


async def test_routines_can_be_switched_off_and_retimed(bob):
    routine = bob.scheduler.configure("notes-tick", {"enabled": False})
    assert not routine.enabled and bob.scheduler.next_run("notes-tick") is None
    routine = bob.scheduler.configure("notes-tick", {"enabled": True, "cron": "0 7 * * *"})
    assert routine.cron == "0 7 * * *" and routine.every_minutes is None
    assert bob.scheduler.next_run("notes-tick") is not None
    with pytest.raises(ValueError):
        bob.scheduler.configure("notes-tick", {"cron": "not a cron"})
    with pytest.raises(ValueError, match="unknown routine setting"):
        bob.scheduler.configure("notes-tick", {"tool": "wipe"})
    with pytest.raises(KeyError):
        bob.scheduler.configure("nope", {"enabled": False})


async def test_a_scheduled_run_outside_its_hours_is_skipped(bob, monkeypatch):
    bob.scheduler.configure("notes-tick", {"between": ["00:00", "00:00"]})  # an empty window
    assert (await bob.scheduler.run("notes-tick", trigger="schedule"))["outcome"] == "skipped"
    assert (await bob.scheduler.run("notes-tick", trigger="manual"))["outcome"] == "ok"


async def test_conformance_passes_and_reports_os_isolation_as_advisory(bob):
    report = await bob.run_conformance()
    assert report.ok and bob.fatal == []
    by_name = {check.name: check for check in report.checks}
    for name in (
        "wrapper refuses git push",
        "wrapper refuses git commit",
        "a push from a clone fails",
        "the pre-push hook alone stops a push",
        "wrapper only runs inside the knowledge root",
        "git allow-list matches the design",
        "no shell or run-command tool exists",
        "no LYIK server credentials in the environment",
    ):
        assert by_name[name].passed, by_name[name].detail
    # The test process can write to the stand-in ~/dev, and says so.
    writable = next(c for c in report.checks if c.level == "macos")
    assert not writable.passed and writable.advisory
    assert list(bob.config.knowledge_root.glob(".conformance-*")) == []  # cleaned up
    assert bob.audit.entries(kind="conformance")[0].outcome == "ok"


async def test_a_write_that_gets_through_shuts_bobd_down(bob):
    bob.config.security.os_isolation = "required"
    report = await bob.run_conformance()
    assert not report.ok
    assert bob.fatal and "Read-only conformance failed" in bob.fatal[0]
    assert bob.audit.entries(kind="conformance")[0].outcome == "FAILED"


async def test_read_only_folder_passes_the_filesystem_check(bob, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        bob.config.security.protected_roots = [str(locked)]
        bob.config.security.os_isolation = "required"
        (check,) = check_filesystem(bob.config)
        assert check.passed and "refused" in check.detail
    finally:
        locked.chmod(0o755)


async def test_shell_like_and_code_writing_tools_fail_conformance(bob):
    from bob_sdk.manifest import parse_manifest

    plugin = bob.plugins.get("rogue")
    plugin.manifest = parse_manifest(
        {
            "id": "rogue", "name": "Rogue", "version": "1", "mcp": "python server.py",
            "tools": {"run_command": "read", "git_push": "draft"},
        }
    )  # fmt: skip
    checks = {check.name: check for check in check_tools(bob.plugins)}
    assert not checks["no shell or run-command tool exists"].passed
    assert not checks["no commit, push or load tool exists"].passed
    assert not run_conformance(bob.config, bob.plugins).ok


def test_config_enforces_local_only(tmp_path):
    for bad in (
        {"ollama": {"base_url": "http://192.168.1.10:11434"}},
        {"ollama": {"base_url": "https://ollama.com"}},
        {"server": {"host": "0.0.0.0"}},
        {"models": {"main": {"model": "qwen3:cloud"}}},
        {"models": {"main": {"model": "gpt-oss:120b-cloud"}}},
        {"models": {"wizard": {"model": "x"}}},
        {
            "knowledge": {
                "collections": [
                    {"id": "a", "sources": [{"github": {"repo": "git@github.com:a/b.git"}}]}
                ]
            }
        },
        {"knowledge": {"collections": [{"id": "a", "sources": [{}]}]}},
        {"knowledge": {"collections": [{"id": "a"}, {"id": "a"}]}},
    ):
        with pytest.raises(ValueError):
            Config.model_validate(bad)
    config = Config.model_validate({"models": {"main": {"model": "qwen3.6:35b-a3b"}}})
    assert config.models["main"].model == "qwen3.6:35b-a3b"
    assert config.models["router"].model == "gemma4:e4b"  # defaults fill the other roles

    file = tmp_path / "config.yaml"
    file.write_text("server: {port: 9999}\npaths: {data: state}\n")
    loaded = load_config(file)
    assert loaded.server.port == 9999 and loaded.data_dir == (tmp_path / "state").resolve()
    file.write_text("server: [1, 2]\n")
    with pytest.raises(ConfigError, match="server"):
        load_config(file)
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")


async def test_a_routine_with_nothing_to_do_is_skipped_not_failed(bob, monkeypatch):
    async def nothing_to_do(_routine):
        return {"skipped": True, "reason": "mail is not set up; run: bob mail setup"}

    monkeypatch.setattr(bob.scheduler, "_execute", nothing_to_do)
    result = await bob.scheduler.run("notes-tick")
    assert result["outcome"] == "skipped" and "bob mail setup" in result["detail"]
    assert bob.scheduler.history("notes-tick")[0]["outcome"] == "skipped"
