"""Reading events from macOS Calendar through EventKit. Read-only: nothing is saved."""

from __future__ import annotations

import threading
from datetime import datetime

from bob_sdk.runtime import ToolError

from .events import Event

ACCESS_HELP = (
    "Bob cannot read your calendar yet. Allow it under System Settings → Privacy & Security "
    "→ Calendars, then try again."
)


class EventKitSource:
    def __init__(self) -> None:
        try:
            import EventKit
        except ImportError as exc:
            raise ToolError("EventKit is only available on macOS") from exc
        self._ek = EventKit
        self._store = EventKit.EKEventStore.alloc().init()

    def _authorised(self) -> bool:
        ek = self._ek
        status = ek.EKEventStore.authorizationStatusForEntityType_(ek.EKEntityTypeEvent)
        if status == ek.EKAuthorizationStatusAuthorized:  # full access
            return True
        if status != ek.EKAuthorizationStatusNotDetermined:
            return False  # denied, restricted or write-only
        # First use: macOS asks you once.
        done = threading.Event()
        granted: list[bool] = []

        def completion(ok: bool, _error) -> None:
            granted.append(bool(ok))
            done.set()

        if hasattr(self._store, "requestFullAccessToEventsWithCompletion_"):
            self._store.requestFullAccessToEventsWithCompletion_(completion)
        else:
            self._store.requestAccessToEntityType_completion_(ek.EKEntityTypeEvent, completion)
        done.wait(120)
        return bool(granted and granted[0])

    def events(self, start: datetime, end: datetime) -> list[Event]:
        import Foundation

        if not self._authorised():
            raise ToolError(ACCESS_HELP)

        def ns_date(value: datetime):
            return Foundation.NSDate.dateWithTimeIntervalSince1970_(value.timestamp())

        def local(ns) -> datetime:
            return datetime.fromtimestamp(ns.timeIntervalSince1970())

        predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
            ns_date(start), ns_date(end), None
        )
        found = self._store.eventsMatchingPredicate_(predicate) or []
        return [
            Event(
                title=str(item.title() or "(no title)"),
                start=local(item.startDate()),
                end=local(item.endDate()),
                all_day=bool(item.isAllDay()),
                location=str(item.location() or ""),
                calendar=str(item.calendar().title() or "") if item.calendar() else "",
            )
            for item in found
        ]
