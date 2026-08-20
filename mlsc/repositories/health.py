"""Persistence for the one health row per monitor source."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import SourceHealth, SourceState


class HealthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, monitor_source_id: uuid.UUID) -> SourceHealth:
        result = await self._session.execute(
            select(SourceHealth).where(SourceHealth.monitor_source_id == monitor_source_id)
        )
        health = result.scalar_one_or_none()
        if health is None:
            health = SourceHealth(id=uuid.uuid4(), monitor_source_id=monitor_source_id)
            self._session.add(health)
        return health

    def save(
        self,
        health: SourceHealth,
        *,
        state: SourceState,
        consecutive_empty: int,
        consecutive_fail: int,
        last_success_at: datetime | None,
        rows_median_28d: float | None,
        library_version: str | None,
    ) -> None:
        health.state = state
        health.consecutive_empty = consecutive_empty
        health.consecutive_fail = consecutive_fail
        health.last_success_at = last_success_at
        health.rows_median_28d = rows_median_28d
        health.library_version = library_version
