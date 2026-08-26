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
        self,
        source_id: uuid.UUID,
        *,
        last_external_id: str | None = None,
        last_published_at: datetime | None = None,
    ) -> None:
        """Advance whichever cursor fields the caller's kind tracks.

        Callers pass the fields of the cursor their adapter returned, and a kind
        tracks only some of them — App Store has no timestamp, the query-driven
        kinds no external id. An omitted field is left as it stands rather than
        written as null, so advancing one does not clear the other
        (all-source-collection design.md, "Cursor write-back reads the new
        cursor's own fields").
        """
        source = await self.get(source_id)
        if last_external_id is not None:
            source.last_external_id = last_external_id
        if last_published_at is not None:
            source.last_published_at = last_published_at
