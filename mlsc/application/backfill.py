"""Backfill submission: a one-shot wider-window collection, kept separate from
the daily series (requirement 6, 7). Reuses collect_source unchanged."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import BackfillJob, BackfillStatus, IngestionRun, MonitorSource


class BackfillOverlaps(RuntimeError):
    """A backfill for this window is already recorded."""


class BackfillService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def submit(
        self, monitor_id: uuid.UUID, window_start: date, window_end: date
    ) -> uuid.UUID:
        if window_start > window_end or window_end > date.today():
            raise ValueError("backfill window must be ordered and not extend into the future")

        async with self._session_factory() as session:
            result = await session.execute(
                select(BackfillJob).where(
                    BackfillJob.monitor_id == monitor_id,
                    BackfillJob.window_start <= window_end,
                    BackfillJob.window_end >= window_start,
                    BackfillJob.status.in_((BackfillStatus.PENDING, BackfillStatus.RUNNING)),
                )
            )
            if result.scalar_one_or_none() is not None:
                raise BackfillOverlaps(str(monitor_id))

            job = BackfillJob(
                id=uuid.uuid4(), monitor_id=monitor_id, window_start=window_start, window_end=window_end
            )
            session.add(job)
            await session.commit()
            return job.id

    async def run(self, job_id: uuid.UUID, *, collect_one_date) -> None:  # noqa: ANN001
        """Fan out one IngestionRun(is_backfill=True) per date in the window.

        Continues past a per-date failure — a backfill that abandons twenty
        days because one failed is not resumable in any useful sense
        (design.md, "Failure strategy").
        """
        async with self._session_factory() as session:
            job = await session.get(BackfillJob, job_id)
            job.status = BackfillStatus.RUNNING
            await session.commit()
            monitor_id, start, end = job.monitor_id, job.window_start, job.window_end

        current = start
        any_failed = False
        while current <= end:
            run_id = uuid.uuid4()
            async with self._session_factory() as session:
                session.add(
                    IngestionRun(id=run_id, monitor_id=monitor_id, run_date=current, is_backfill=True)
                )
                await session.commit()
                sources_result = await session.execute(
                    select(MonitorSource).where(
                        MonitorSource.monitor_id == monitor_id, MonitorSource.enabled.is_(True)
                    )
                )
                sources = list(sources_result.scalars().all())
            for source in sources:
                try:
                    await collect_one_date(run_id, source.id)
                except Exception:  # noqa: BLE001 - one date's failure must not abort the window
                    any_failed = True
            current += timedelta(days=1)

        async with self._session_factory() as session:
            job = await session.get(BackfillJob, job_id)
            job.status = BackfillStatus.FAILED if any_failed else BackfillStatus.COMPLETE
            await session.commit()
