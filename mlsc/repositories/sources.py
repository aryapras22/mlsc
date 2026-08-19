"""Persistence for monitor sources.

Takes a caller-owned ``AsyncSession`` and never commits; the service owns the
transaction boundary, matching ``mlsc/repositories/monitors.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import MonitorSource


class MonitorSourceNotFound(KeyError):
    """Raised when a ``SourceId`` does not resolve to a stored source."""

    def __init__(self, source_id: uuid.UUID) -> None:
        super().__init__(str(source_id))
        self.source_id = source_id


class MonitorSourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def insert(self, source: MonitorSource) -> None:
        self._session.add(source)

    async def get(self, source_id: uuid.UUID) -> MonitorSource:
        source = await self._session.get(
            MonitorSource, source_id, populate_existing=True
        )
        if source is None:
            raise MonitorSourceNotFound(source_id)
        return source

    async def list_for_monitor(self, monitor_id: uuid.UUID) -> list[MonitorSource]:
        result = await self._session.execute(
            select(MonitorSource)
            .where(MonitorSource.monitor_id == monitor_id)
            .order_by(MonitorSource.created_at)
        )
        return list(result.scalars().all())

    async def save_cursor(
        self, source_id: uuid.UUID, *, last_external_id: str, last_published_at: datetime
    ) -> None:
        source = await self.get(source_id)
        source.last_external_id = last_external_id
        source.last_published_at = last_published_at
