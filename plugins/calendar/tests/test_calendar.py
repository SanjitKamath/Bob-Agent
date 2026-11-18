from __future__ import annotations

from datetime import date, datetime, time

import pytest
from bob_calendar.events import Event, events_between, free_slots, parse_clock, parse_day
from bob_sdk.runtime import ToolError, check_declared

DAY = date(2026, 11, 13)


def event(title, start, end, **kwargs):
    return Event(title, datetime(2026, 11, 13, *start), datetime(2026, 11, 13, *end), **kwargs)


class FakeSource:
    def __init__(self, events):
        self._events = events
        self.asked = None

    def events(self, start, end):
        self.asked = (start, end)
        return [e for e in self._events if e.end > start and e.start < end]


def slots(events, minutes=30):
    found = free_slots(events, DAY, day_start=time(9), day_end=time(18), minutes=minutes)
    return [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in found]


def test_free_slots_between_meetings():
    events = [event("Standup", (10, 0), (10, 30)), event("Review", (14, 0), (15, 0))]
    assert slots(events) == [("09:00", "10:00"), ("10:30", "14:00"), ("15:00", "18:00")]


def test_overlaps_edges_and_all_day_events():
    events = [
        event("Early", (8, 0), (9, 30)),  # starts before the day
        event("A", (11, 0), (12, 0)),
        event("B overlaps A", (11, 30), (12, 15)),
        event("Inside A", (11, 10), (11, 20)),
        event("Short gap after", (12, 30), (17, 50)),  # leaves 15 and 10 minutes
        event("Holiday", (0, 0), (23, 59), all_day=True),
    ]
    assert slots(events) == [("09:30", "11:00")]
    assert slots(events, minutes=10) == [("09:30", "11:00"), ("12:15", "12:30"), ("17:50", "18:00")]
    assert slots([]) == [("09:00", "18:00")]
    assert slots([event("All", (9, 0), (18, 0))]) == []
    with pytest.raises(ToolError):
        slots([], minutes=1)
    with pytest.raises(ToolError, match="end after it starts"):
        free_slots([], DAY, day_start=time(18), day_end=time(9))


def test_ranges_are_whole_days_sorted_and_bounded():
    source = FakeSource([event("Late", (16, 0), (17, 0)), event("First", (9, 0), (10, 0))])
    found = events_between(source, DAY, DAY)
    assert [e.title for e in found] == ["First", "Late"]
    assert source.asked == (datetime(2026, 11, 13), datetime(2026, 11, 14))
    assert found[0].as_dict()["start"] == "2026-11-13T09:00"
    with pytest.raises(ToolError, match="before the start"):
        events_between(source, DAY, date(2026, 11, 12))
    with pytest.raises(ToolError, match="at most"):
        events_between(source, DAY, date(2027, 6, 1))


def test_parsing():
    assert parse_day("2026-11-13") == DAY and parse_day(None) == date.today()
    assert parse_clock("09:30", "day_start") == time(9, 30)
    for bad in ("13/11/2026", "next monday"):
        with pytest.raises(ToolError):
            parse_day(bad)
    with pytest.raises(ToolError, match="HH:MM"):
        parse_clock("9am", "day_start")


def test_server_tools_use_the_source(monkeypatch, tmp_path):
    import json

    from bob_calendar import server

    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"data_dir": str(tmp_path / "d"), "settings": {"day_end": "12:00"}})
    )
    monkeypatch.setenv("BOB_PLUGIN_CONFIG", str(config))
    monkeypatch.setattr(server, "_ctx", None)
    monkeypatch.setattr(server, "_source", FakeSource([event("Standup", (10, 0), (10, 30))]))
    result = server.find_free_slots("2026-11-13", 30)
    assert result["slots"] == [
        {"start": "2026-11-13T09:00", "end": "2026-11-13T10:00", "minutes": 60},
        {"start": "2026-11-13T10:30", "end": "2026-11-13T12:00", "minutes": 90},
    ]
    assert server.events_in_range("2026-11-13", "2026-11-13")["events"][0]["title"] == "Standup"
    check_declared(server.server, server.MANIFEST)
