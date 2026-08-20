"""Retention enforcement: delete documents past a monitor's retention window.

Open decision from the spec, recorded here: no trend-detection or
insight-generation tables exist yet to reference a document as evidence, so
the evidence-retention rule is currently a no-op — every expired document is
free to delete. Revisit when those tables land.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import Document, Monitor


@dataclasses.dataclass(frozen=True)
class RetentionOutcome:
    monitor_id: uuid.UUID
    cutoff: date
    documents_removed: int
    evidence_retained: int = 0


async def enforce_retention(
    session_factory: async_sessionmaker[AsyncSession], monitor_id: uuid.UUID
) -> RetentionOutcome:
    async with session_factory() as session:
        monitor = await session.get(Monitor, monitor_id)
        cutoff = date.today() - timedelta(days=monitor.retention_days)

        count_result = await session.execute(
            select(Document.id).where(
                Document.monitor_id == monitor_id, Document.published_at < cutoff
            )
        )
        expired_ids = [row[0] for row in count_result.all()]

        if expired_ids:
            await session.execute(delete(Document).where(Document.id.in_(expired_ids)))
            await session.commit()

        return RetentionOutcome(
            monitor_id=monitor_id, cutoff=cutoff, documents_removed=len(expired_ids)
        )
