"""Read queries for run detail and run history."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import FetchStats, IngestionRun


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, run_id: uuid.UUID) -> IngestionRun | None:
        return await self._session.get(IngestionRun, run_id)

    async def stats_for_run(self, run_id: uuid.UUID) -> list[FetchStats]:
        result = await self._session.execute(
            select(FetchStats).where(FetchStats.run_id == run_id)
        )
        return list(result.scalars().all())

    async def history_for_monitor(self, monitor_id: uuid.UUID) -> list[IngestionRun]:
        result = await self._session.execute(
            select(IngestionRun)
            .where(IngestionRun.monitor_id == monitor_id)
            .order_by(IngestionRun.run_date.desc())
        )
        return list(result.scalars().all())
