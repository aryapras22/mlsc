"""Persistence for monitors and their schedule registrations.

Repositories take a caller-owned ``AsyncSession`` and never commit; the service
owns the transaction boundary (design.md, "Dependencies, injected").
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mlsc.db.models import Monitor, MonitorStatus, ScheduleRegistration


class MonitorNotFound(KeyError):
    """Raised when a ``MonitorId`` does not resolve to a stored monitor."""

    def __init__(self, monitor_id: uuid.UUID) -> None:
        super().__init__(str(monitor_id))
        self.monitor_id = monitor_id


class MonitorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def insert(self, monitor: Monitor) -> None:
        self._session.add(monitor)

    async def get(self, monitor_id: uuid.UUID) -> Monitor:
        # populate_existing=True: without it, session.get() returns an
        # already-identity-mapped Monitor as-is and skips the eager load below,
        # so a caller that inserted or updated the row in this same session
        # would see a stale (unloaded) `registration` relationship.
        monitor = await self._session.get(
            Monitor,
            monitor_id,
            options=(selectinload(Monitor.registration),),
            populate_existing=True,
        )
        if monitor is None:
            raise MonitorNotFound(monitor_id)
        return monitor

    async def list_all(self) -> list[Monitor]:
        result = await self._session.execute(
            select(Monitor).options(selectinload(Monitor.registration)).order_by(Monitor.created_at)
        )
        return list(result.scalars().all())


class ScheduleRegistrationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_active(
        self, monitor_id: uuid.UUID, cron_expression: str, timezone: str
    ) -> None:
        """Insert or update the one registration a monitor may have while active."""
        result = await self._session.execute(
            select(ScheduleRegistration).where(ScheduleRegistration.monitor_id == monitor_id)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            self._session.add(
                ScheduleRegistration(
                    monitor_id=monitor_id, cron_expression=cron_expression, timezone=timezone
                )
            )
        else:
            existing.cron_expression = cron_expression
            existing.timezone = timezone

    async def delete_all_for(self, monitor_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(ScheduleRegistration).where(ScheduleRegistration.monitor_id == monitor_id)
        )

    async def list_active(self) -> list[ScheduleRegistration]:
        result = await self._session.execute(
            select(ScheduleRegistration)
            .join(Monitor)
            .where(Monitor.status == MonitorStatus.ACTIVE)
        )
        return list(result.scalars().all())
