"""Deterministic schedule projection and Celery Beat integration only.

Beat asks the database which monitors are active and what their schedule is on
every tick, rather than reading a static ``beat_schedule`` dict defined at
import time (learn.md, "Durable schedule projection"). Projection is
recomputed from scratch each tick; nothing here is incremental.
"""

from __future__ import annotations

import asyncio
import logging
import zoneinfo

from croniter import croniter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celery.beat import Scheduler
from celery.schedules import crontab, schedule
from mlsc.repositories.monitors import ScheduleRegistrationRepository

logger = logging.getLogger(__name__)

# The five multi-monitor cadences from design.md's "Success path", none of
# which take a monitor argument. Times are spread across off-peak UTC hours
# so they don't collide with each other or with a monitor's own collection
# schedule: retention daily at 02:00, discovery weekly Sunday 03:00, snapshot
# weekly Sunday 04:00, refit monthly on the 1st at 05:00. Alert delivery is
# the one interval-based entry, every 15 minutes, since pending deliveries
# need to reach a terminal state well within a day (requirement 3).
STATIC_SCHEDULE: dict[str, dict[str, object]] = {
    "cadence:alert-delivery": {
        "task": "mlsc.run_alert_delivery",
        "schedule": schedule(run_every=900),
        "args": (),
    },
    "cadence:weekly-discovery": {
        "task": "mlsc.run_weekly_discovery",
        "schedule": crontab(minute=0, hour=3, day_of_week=0),
        "args": (),
    },
    "cadence:monthly-refit": {
        "task": "mlsc.run_monthly_refit",
        "schedule": crontab(minute=0, hour=5, day_of_month=1),
        "args": (),
    },
    "cadence:retention-sweep": {
        "task": "mlsc.run_retention_sweep",
        "schedule": crontab(minute=0, hour=2),
        "args": (),
    },
    "cadence:stability-snapshot": {
        "task": "mlsc.run_stability_snapshot",
        "schedule": crontab(minute=0, hour=4, day_of_week=0),
        "args": (),
    },
}


class _TzAwareCrontab(crontab):
    """A ``crontab`` schedule whose timezone comes from the stored registration.

    ``celery.schedules.BaseSchedule.tz`` is normally the Celery app's single
    global timezone; each monitor needs its own, so this overrides it per
    instance rather than per app.
    """

    def __init__(self, *args: object, tz_name: str, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._tz_name = tz_name

    @property
    def tz(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self._tz_name)


def _crontab_from_expression(cron_expression: str, tz_name: str) -> crontab:
    minute, hour, day_of_month, month_of_year, day_of_week = cron_expression.split()
    return _TzAwareCrontab(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
        tz_name=tz_name,
    )


class SchedulePlanner:
    """Projects Beat's schedule from persisted, active registrations only."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def project(self) -> dict[str, dict[str, object]]:
        """Build one Beat entry per active registration, keyed ``monitor:{id}``.

        A registration whose stored expression no longer parses is skipped and
        logged rather than raised, so one corrupt row does not silence every
        other monitor's projection (design.md, "Failure strategy").
        """
        async with self._session_factory() as session:
            registrations = await ScheduleRegistrationRepository(session).list_active()

        entries: dict[str, dict[str, object]] = {}
        for registration in registrations:
            if not croniter.is_valid(registration.cron_expression):
                logger.warning(
                    "skipping unprojectable schedule for monitor %s: %r",
                    registration.monitor_id,
                    registration.cron_expression,
                )
                continue
            key = f"monitor:{registration.monitor_id}"
            entries[key] = {
                "task": "mlsc.run_monitor",
                "schedule": _crontab_from_expression(
                    registration.cron_expression, registration.timezone
                ),
                "args": (str(registration.monitor_id),),
            }
        return entries


class MonitorAwareScheduler(Scheduler):
    """Beat scheduler that re-derives its schedule from ``SchedulePlanner`` each tick.

    Celery's default scheduler treats ``schedule`` as static once loaded; this
    override re-projects from persisted state before every tick, so a pause or
    resume 30 seconds ago is honoured on the very next tick rather than
    requiring a restart (learn.md, "Durable schedule projection").

    One event loop is created and reused for every tick. asyncpg connections
    are bound to the loop that created them; calling ``asyncio.run`` per tick
    would tear that loop down each time and the next tick's query would fail
    with "attached to a different loop", even with a ``NullPool`` engine.
    """

    def __init__(self, *args: object, planner: SchedulePlanner | None = None, **kwargs: object) -> None:
        self._planner = planner or _default_planner()
        self._loop = asyncio.new_event_loop()
        super().__init__(*args, **kwargs)

    def setup_schedule(self) -> None:
        super().setup_schedule()
        self._sync_from_planner()

    def tick(self, *args: object, **kwargs: object) -> float:
        self._sync_from_planner()
        return super().tick(*args, **kwargs)

    def _sync_from_planner(self) -> None:
        # merge_inplace pops every schedule key not present in what's passed
        # to it (celery.beat.Scheduler.merge_inplace), so the five static
        # entries must be included on every tick, not just at setup time, or
        # they are deleted on the tick after the one that added them.
        dynamic_entries = self._loop.run_until_complete(self._planner.project())
        self.merge_inplace({**STATIC_SCHEDULE, **dynamic_entries})

    def close(self) -> None:
        super().close()
        self._loop.close()


def _default_planner() -> SchedulePlanner:
    """Build a planner with its own ``NullPool`` engine.

    Beat ticks are minutes apart, so a pooled, idle connection would likely be
    dropped by the server between ticks anyway; a fresh connection per tick is
    simpler than keeping a pool alive for a query this infrequent.
    """
    from sqlalchemy import pool
    from sqlalchemy.ext.asyncio import create_async_engine

    from mlsc.config import load_settings
    from mlsc.db.session import _ssl_argument, build_session_factory, build_url

    settings = load_settings().postgres
    engine = create_async_engine(
        build_url(settings),
        connect_args={
            "timeout": settings.connect_timeout_seconds,
            "command_timeout": settings.command_timeout_seconds,
            "ssl": _ssl_argument(settings),
        },
        poolclass=pool.NullPool,
    )
    return SchedulePlanner(build_session_factory(engine))
