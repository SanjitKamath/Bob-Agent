"""MCP server for the Calendar plugin. Every tool is read-only."""

from __future__ import annotations

from typing import Any

from bob_sdk.runtime import PluginContext, create_server, manifest_for, run

from .events import EventSource, day_bounds, events_between, free_slots, parse_clock, parse_day

MANIFEST = manifest_for(__file__)
server = create_server(MANIFEST)
_ctx: PluginContext | None = None
_source: EventSource | None = None


def context() -> PluginContext:
    global _ctx
    if _ctx is None:
        _ctx = PluginContext.load(MANIFEST)
    return _ctx


def source() -> EventSource:
    global _source
    if _source is None:
        from .eventkit import EventKitSource

        _source = EventKitSource()
    return _source


@server.tool()
def today() -> dict[str, Any]:
    """Today's calendar events, in order."""
    day = parse_day("today")
    events = events_between(source(), day, day)
    return {"date": day.isoformat(), "events": [event.as_dict() for event in events]}


@server.tool(name="range")
def events_in_range(start: str, end: str) -> dict[str, Any]:
    """Events from one date to another (YYYY-MM-DD), inclusive."""
    first, last = parse_day(start), parse_day(end)
    events = events_between(source(), first, last)
    return {
        "start": first.isoformat(),
        "end": last.isoformat(),
        "events": [event.as_dict() for event in events],
    }


@server.tool(name="free_slots")
def find_free_slots(date: str = "today", minutes: int = 30) -> dict[str, Any]:
    """Free time inside the working day on a date, in slots of at least the given minutes."""
    day = parse_day(date)
    settings = context().settings
    start, end = day_bounds(day)
    slots = free_slots(
        source().events(start, end),
        day,
        day_start=parse_clock(settings.get("day_start") or "09:00", "day_start"),
        day_end=parse_clock(settings.get("day_end") or "18:00", "day_end"),
        minutes=minutes,
    )
    return {
        "date": day.isoformat(),
        "slots": [
            {
                "start": a.isoformat(timespec="minutes"),
                "end": b.isoformat(timespec="minutes"),
                "minutes": int((b - a).total_seconds() // 60),
            }
            for a, b in slots
        ],
    }


def main() -> None:
    run(server, MANIFEST)


if __name__ == "__main__":
    main()
