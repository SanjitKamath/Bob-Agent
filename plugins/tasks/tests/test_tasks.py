from __future__ import annotations

from datetime import date, timedelta

import pytest
from bob_sdk.runtime import ToolError
from bob_tasks.store import TaskStore, parse_due, parse_task_lines


@pytest.fixture
def store(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite")
    yield store
    store.close()


def test_open_tasks_are_ordered_by_priority_then_due(store):
    store.create("low one", priority="low")
    store.create("later", due="2030-01-02")
    store.create("sooner", due="2030-01-01")
    store.create("urgent", priority="high")
    assert [t["title"] for t in store.list()] == ["urgent", "sooner", "later", "low one"]


def test_complete_reopen_and_status_filters(store):
    task = store.create("write report", due="today")
    assert task["due"] == date.today().isoformat() and task["completed_at"] is None
    assert store.complete(task["id"])["completed_at"]
    assert store.list("open") == [] and [t["id"] for t in store.list("done")] == [task["id"]]
    assert store.complete(task["id"], done=False)["completed_at"] is None
    with pytest.raises(ToolError, match="no task 99"):
        store.complete(99)


def test_update_changes_only_given_fields(store):
    task = store.create("call bank", due="tomorrow", priority="high", notes="re: loan")
    updated = store.update(task["id"], title="  call   the bank ")
    assert (updated["title"], updated["priority"], updated["notes"]) == (
        "call the bank",
        "high",
        "re: loan",
    )
    assert updated["due"] == (date.today() + timedelta(days=1)).isoformat()
    assert store.update(task["id"], due="")["due"] is None
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        store.update(task["id"], due="next week")
    with pytest.raises(ToolError, match="needs a title"):
        store.update(task["id"], title="   ")
    with pytest.raises(ToolError, match="priority"):
        store.create("x", priority="urgent")  # type: ignore[arg-type]


def test_summary_counts_for_the_widget(store):
    assert store.summary() == {
        "counts": {"open": 0, "due_today": 0, "overdue": 0},
        "status": "Nothing open",
        "items": [],
    }
    store.create("old", due="2020-01-01")
    store.create("now", due="today", priority="high")
    done = store.create("finished")
    store.complete(done["id"])
    summary = store.summary()
    assert summary["counts"] == {"open": 2, "due_today": 1, "overdue": 1}
    assert summary["status"] == "Next: now"
    # Every open task is listed, most pressing first, with when it is due and whether it is late.
    assert summary["items"] == [
        {"title": "now", "detail": "high priority, due today", "flag": "today"},
        {"title": "old", "detail": "due 2020-01-01", "flag": "overdue"},
    ]


def test_parsing_helpers():
    assert parse_due(None) is None and parse_due("  ") is None
    assert parse_due("2026-11-13T10:00:00") == "2026-11-13"
    text = "Intro\n- Send the KYC form\n2. Review PR 12\n* send the kyc form\n- none\n[ ] Book flights\nplain"
    assert parse_task_lines(text) == [
        "Send the KYC form", "Review PR 12", "send the kyc form", "Book flights",
    ]  # fmt: skip


async def test_extract_creates_tasks_without_the_core(tmp_path, monkeypatch):
    import json

    from bob_tasks import server

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"data_dir": str(tmp_path / "data")}))
    monkeypatch.setenv("BOB_PLUGIN_CONFIG", str(config))
    monkeypatch.setattr(server, "_ctx", None)
    monkeypatch.setattr(server, "_store", TaskStore(tmp_path / "t.sqlite"))
    result = await server.extract_from("Todo:\n- Renew passport\n- Email Priya", source="note")
    assert [t["title"] for t in result["created"]] == ["Renew passport", "Email Priya"]
    assert result["created"][0]["source"] == "note"
    with pytest.raises(ToolError):
        await server.extract_from("   ")


def test_server_declares_exactly_its_tools():
    from bob_sdk.runtime import check_declared
    from bob_tasks import server

    check_declared(server.server, server.MANIFEST)
