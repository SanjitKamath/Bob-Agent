"""Calendar events and free-slot arithmetic, independent of where events come from."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from bob_sdk.runtime import ToolError

MAX_RANGE_DAYS = 62


@dataclass(frozen=True)
class Event:
    title: str
    start: datetime  # local time, naive
    end: datetime
    all_day: bool = False
    location: str = ""
    calendar: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "start": self.start.isoformat(timespec="minutes"),
            "end": self.end.isoformat(timespec="minutes"),
            "all_day": self.all_day,
            "location": self.location,
            "calendar": self.calendar,
        }


class EventSource(Protocol):
    def events(self, start: datetime, end: datetime) -> list[Event]: ...


def parse_day(value: str | None) -> date:
    if value is None or not value.strip() or value.strip().lower() == "today":
        return date.today()
    if value.strip().lower() == "tomorrow":
        return date.today() + timedelta(days=1)
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise ToolError(f"date {value!r} must be YYYY-MM-DD, 'today' or 'tomorrow'") from None


def parse_clock(value: str, name: str) -> time:
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(f"{name} {value!r} must be HH:MM") from None


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def events_between(source: EventSource, start_day: date, end_day: date) -> list[Event]:
    if end_day < start_day:
        raise ToolError("the end date is before the start date")
    if (end_day - start_day).days > MAX_RANGE_DAYS:
        raise ToolError(f"a range can cover at most {MAX_RANGE_DAYS} days")
    start, _ = day_bounds(start_day)
    _, end = day_bounds(end_day)
    return sorted(source.events(start, end), key=lambda e: (e.start, e.end, e.title))


def free_slots(
    events: list[Event], day: date, *, day_start: time, day_end: time, minutes: int = 30
) -> list[tuple[datetime, datetime]]:
    """Gaps of at least ``minutes`` inside the working day. All-day events do not block."""
    if minutes < 5:
        raise ToolError("a slot must be at least 5 minutes")
    window_start = datetime.combine(day, day_start)
    window_end = datetime.combine(day, day_end)
    if window_end <= window_start:
        raise ToolError("the working day must end after it starts")
    busy = sorted(
        (max(e.start, window_start), min(e.end, window_end))
        for e in events
        if not e.all_day and e.end > window_start and e.start < window_end
    )
    slots: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for start, end in busy:
        if start - cursor >= timedelta(minutes=minutes):
            slots.append((cursor, start))
        cursor = max(cursor, end)
    if window_end - cursor >= timedelta(minutes=minutes):
        slots.append((cursor, window_end))
    return slots
