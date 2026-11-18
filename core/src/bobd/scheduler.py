"""Routines: jobs that run the same tools and agents without you asking.

APScheduler runs inside bobd with its job store in SQLite, so a job missed
while the Mac slept runs once after it wakes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from bob_sdk.manifest import RoutineSpec

from .broker import Caller, ToolBroker, ToolDenied, ToolFailed
from .config import Config
from .db import Database, utcnow
from .events import Event, EventBus
from .plugins import PluginManager
from .telemetry import Telemetry

log = logging.getLogger(__name__)

_SCOPE = "routines"
MISFIRE_GRACE_S = 12 * 3600

PromptRunner = Callable[[str, str, str], Awaitable[str]]  # (plugin id, routine id, prompt) -> text
CoreJob = Callable[[], Awaitable[dict[str, Any]]]

# APScheduler pickles job callables, so jobs call this module-level function,
# which finds the live scheduler here.
_ACTIVE: RoutineScheduler | None = None


async def run_routine_job(routine_id: str) -> None:
    if _ACTIVE is not None:
        await _ACTIVE.run(routine_id, trigger="schedule")


@dataclass(frozen=True)
class Routine:
    id: str
    title: str
    owner: str  # plugin id, or "core"
    spec: RoutineSpec | None  # None for core jobs
    enabled: bool
    cron: str | None
    every_minutes: int | None
    between: tuple[str, str] | None
    on_event: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "owner": self.owner,
            "enabled": self.enabled,
            "cron": self.cron,
            "every_minutes": self.every_minutes,
            "between": list(self.between) if self.between else None,
            "on_event": self.on_event,
        }


def in_window(between: tuple[str, str] | None, now: datetime) -> bool:
    """Whether ``now`` falls inside a daily HH:MM window (which may cross midnight)."""
    if between is None:
        return True
    start, end = (time.fromisoformat(value) for value in between)
    current = now.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= current < end
    return current >= start or current < end


class RoutineScheduler:
    def __init__(
        self,
        config: Config,
        db: Database,
        plugins: PluginManager,
        broker: ToolBroker,
        events: EventBus,
        telemetry: Telemetry,
        *,
        prompt_runner: PromptRunner | None = None,
        persistent: bool = True,
    ) -> None:
        self._config = config
        self._db = db
        self._plugins = plugins
        self._broker = broker
        self._events = events
        self._telemetry = telemetry
        self._prompt_runner = prompt_runner
        self._core_jobs: dict[str, tuple[Routine, CoreJob]] = {}
        self._unsubscribe: list[Callable[[], None]] = []
        jobstores = {}
        if persistent:
            config.data_dir.mkdir(parents=True, exist_ok=True)
            jobstores["default"] = SQLAlchemyJobStore(
                url=f"sqlite:///{config.data_dir / 'scheduler.sqlite'}"
            )
        self._scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": MISFIRE_GRACE_S,
            },
        )

    # -- what exists ------------------------------------------------------------

    def add_core_job(self, routine_id: str, title: str, cron: str, job: CoreJob) -> None:
        """A job owned by the core itself, such as the nightly conformance test."""
        routine = Routine(routine_id, title, "core", None, True, cron, None, None, None)
        self._core_jobs[routine_id] = (routine, job)

    def _overrides(self, routine_id: str) -> dict[str, Any]:
        merged = dict(self._config.routines.get(routine_id, {}))
        merged.update(self._db.get_setting(_SCOPE, routine_id, {}))
        return merged

    def _resolve(self, routine: Routine) -> Routine:
        override = self._overrides(routine.id)
        cron = override.get("cron", routine.cron)
        every = override.get("every_minutes", routine.every_minutes)
        if "cron" in override:
            every = None
        elif "every_minutes" in override:
            cron = None
        between = override.get("between", routine.between)
        return Routine(
            id=routine.id,
            title=routine.title,
            owner=routine.owner,
            spec=routine.spec,
            enabled=bool(override.get("enabled", routine.enabled)),
            cron=cron,
            every_minutes=every,
            between=tuple(between) if between else None,
            on_event=routine.on_event,
        )

    def routines(self) -> list[Routine]:
        """Routines of running plugins plus the core's own, with your overrides applied."""
        found: list[Routine] = []
        for plugin in self._plugins.running():
            for spec in plugin.manifest.routines:
                found.append(
                    self._resolve(
                        Routine(
                            id=spec.id,
                            title=spec.title or spec.id,
                            owner=plugin.id,
                            spec=spec,
                            enabled=spec.enabled,
                            cron=spec.cron,
                            every_minutes=spec.every_minutes,
                            between=spec.between,
                            on_event=spec.on_event,
                        )
                    )
                )
        found.extend(self._resolve(routine) for routine, _ in self._core_jobs.values())
        return found

    def get(self, routine_id: str) -> Routine:
        for routine in self.routines():
            if routine.id == routine_id:
                return routine
        raise KeyError(f"unknown routine {routine_id!r}")

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        global _ACTIVE
        _ACTIVE = self
        self._scheduler.start()
        self.sync()

    def shutdown(self) -> None:
        global _ACTIVE
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        if _ACTIVE is self:
            _ACTIVE = None

    def sync(self) -> None:
        """Make the scheduled jobs match the current routines.

        Called at start and whenever a plugin is switched on or off or a
        routine is edited, so changes apply with no restart.
        """
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        wanted: dict[str, Routine] = {r.id: r for r in self.routines() if r.enabled}
        for job in self._scheduler.get_jobs():
            if job.id not in wanted or wanted[job.id].on_event:
                self._scheduler.remove_job(job.id)
        for routine in wanted.values():
            if routine.on_event:
                self._unsubscribe.append(
                    self._events.on(routine.on_event, self._event_handler(routine.id))
                )
                continue
            if routine.cron:
                trigger = CronTrigger.from_crontab(routine.cron)
            elif routine.every_minutes:
                trigger = IntervalTrigger(minutes=routine.every_minutes)
            else:
                continue
            existing = self._scheduler.get_job(routine.id)
            if existing is not None and str(existing.trigger) == str(trigger):
                continue  # unchanged: keep its next run time, so missed runs still catch up
            self._scheduler.add_job(
                run_routine_job,
                trigger,
                args=[routine.id],
                id=routine.id,
                name=routine.title,
                replace_existing=True,
            )

    def _event_handler(self, routine_id: str):
        async def handler(_event: Event) -> None:
            await self.run(routine_id, trigger="event")

        return handler

    def configure(self, routine_id: str, changes: dict[str, Any]) -> Routine:
        """Switch a routine on or off or change its time. Applies immediately."""
        self.get(routine_id)
        allowed = {"enabled", "cron", "every_minutes", "between"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown routine setting(s): {sorted(unknown)}")
        if "cron" in changes and changes["cron"] is not None:
            CronTrigger.from_crontab(changes["cron"])  # raises ValueError when malformed
        if changes.get("every_minutes") is not None and int(changes["every_minutes"]) < 1:
            raise ValueError("every_minutes must be at least 1")
        if changes.get("between"):
            for value in changes["between"]:
                time.fromisoformat(value)
        stored = dict(self._db.get_setting(_SCOPE, routine_id, {}))
        stored.update({key: value for key, value in changes.items() if value is not None})
        self._db.set_setting(_SCOPE, routine_id, stored)
        self.sync()
        return self.get(routine_id)

    def next_run(self, routine_id: str) -> str | None:
        job = self._scheduler.get_job(routine_id) if self._scheduler.running else None
        return job.next_run_time.isoformat() if job and job.next_run_time else None

    # -- running --------------------------------------------------------------------

    async def run(self, routine_id: str, *, trigger: str = "manual") -> dict[str, Any]:
        """Run one routine now and record the run."""
        try:
            routine = self.get(routine_id)
        except KeyError:
            log.info("routine %s no longer exists; skipping", routine_id)
            return {"routine": routine_id, "outcome": "skipped", "detail": "unknown routine"}
        if trigger == "schedule" and not in_window(routine.between, datetime.now()):
            return {"routine": routine_id, "outcome": "skipped", "detail": "outside its hours"}
        cursor = self._db.execute(
            "INSERT INTO routine_runs(routine_id, started_at) VALUES (?, ?)", (routine_id, utcnow())
        )
        run_id = cursor.lastrowid
        outcome, detail, result = "ok", "", None
        try:
            with self._telemetry.span(
                "routine", {"bob.routine": routine_id, "bob.trigger": trigger}
            ):
                result = await self._execute(routine)
            if isinstance(result, dict) and result.get("ok") is False:
                outcome, detail = "failed", str(result.get("detail") or result.get("error") or "")
            elif isinstance(result, dict) and result.get("skipped") is True:
                # Nothing to do yet (an account not set up, say): not a failure to notify about.
                outcome, detail = "skipped", str(result.get("reason") or "")
        except (ToolDenied, ToolFailed) as exc:
            outcome, detail = "error", str(exc)
        except Exception as exc:
            log.exception("routine %s crashed", routine_id)
            outcome, detail = "error", f"{exc.__class__.__name__}: {exc}"
        self._db.execute(
            "UPDATE routine_runs SET finished_at=?, outcome=?, detail=? WHERE id=?",
            (utcnow(), outcome, detail[:2000], run_id),
        )
        self._telemetry.routine_run(routine_id, outcome)
        await self._events.publish(
            "routine.finished", routine=routine_id, outcome=outcome, detail=detail[:500]
        )
        return {"routine": routine_id, "outcome": outcome, "detail": detail, "result": result}

    async def _execute(self, routine: Routine) -> Any:
        if routine.spec is None:
            return await self._core_jobs[routine.id][1]()
        if routine.spec.prompt is not None:
            if self._prompt_runner is None:
                raise ToolFailed("prompt routines are not available")
            text = await self._prompt_runner(routine.owner, routine.id, routine.spec.prompt)
            cursor = self._db.execute(
                "INSERT INTO cards(created_at, kind, title, body, source) VALUES (?, ?, ?, ?, ?)",
                (utcnow(), routine.spec.card or "routine", routine.title, text, routine.id),
            )
            await self._events.publish(
                "card.created", id=cursor.lastrowid, kind=routine.spec.card or "routine",
                title=routine.title,
            )  # fmt: skip
            return {"ok": True, "card": cursor.lastrowid}
        assert routine.spec.tool is not None
        return await self._broker.call(
            routine.owner,
            routine.spec.tool,
            routine.spec.args,
            Caller(kind="routine", name=routine.id, plugin=routine.owner),
        )

    def history(self, routine_id: str | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM routine_runs", []
        if routine_id:
            sql += " WHERE routine_id=?"
            params.append(routine_id)
        rows = self._db.query(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])
        return [dict(row) for row in rows]
