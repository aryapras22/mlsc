"""Submit and record operator-initiated repairs.

Overlap is refused before any row is written: two purges racing on the same
rows, or two backfills on the same window, would produce work whose outcome
nobody could attribute (design.md, "Failure strategy").
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import OverrideJob, OverrideKind, OverrideStatus
from mlsc.schemas.overrides import OverrideRequest

_IN_FLIGHT = (OverrideStatus.PENDING, OverrideStatus.RUNNING)


class Clock(Protocol):
    def today(self) -> date: ...


class _SystemClock:
    def today(self) -> date:
        return date.today()


class OverrideOverlaps(RuntimeError):
    """Raised when an override of the same kind is already in flight for
    this monitor. Carries the running job's id so the caller can name it."""

    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(str(job_id))
        self.job_id = job_id


class OverrideDispatcher(Protocol):
    """Injected so this service never imports Celery directly (design.md,
    "Dependencies, injected")."""

    def dispatch_override(self, job_id: uuid.UUID) -> None: ...


def _parameters_for(request: OverrideRequest) -> dict[str, Any]:
    if request.kind is OverrideKind.STAGE_RERUN:
        assert request.stage is not None
        return {"stage": request.stage.value}
    if request.kind is OverrideKind.BACKFILL_WINDOW:
        assert request.window_start is not None and request.window_end is not None
        return {
            "window_start": request.window_start.isoformat(),
            "window_end": request.window_end.isoformat(),
        }
    return {}


class OverrideService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher: OverrideDispatcher,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._dispatcher = dispatcher
        self._clock = clock or _SystemClock()

    async def submit(self, monitor_id: uuid.UUID, request: OverrideRequest) -> uuid.UUID:
        async with self._session_factory() as session:
            existing = await session.execute(
                select(OverrideJob).where(
                    OverrideJob.monitor_id == monitor_id,
                    OverrideJob.kind == request.kind,
                    OverrideJob.status.in_(_IN_FLIGHT),
                )
            )
            in_flight = existing.scalar_one_or_none()
            if in_flight is not None:
                raise OverrideOverlaps(in_flight.id)

            job = OverrideJob(
                id=uuid.uuid4(),
                monitor_id=monitor_id,
                kind=request.kind,
                parameters=_parameters_for(request),
                status=OverrideStatus.PENDING,
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        self._dispatcher.dispatch_override(job_id)
        return job_id
