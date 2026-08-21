"""Resolves a caller's topic and source filters against one monitor.

A merged topic resolves through lineage to the identifier it survives under
(design.md, "Success path": "topic resolved through lineage if merged") —
C3 promises an old identifier stays resolvable, and this is where that
promise is redeemed for a read. An unknown filter is a named failure, never
an empty result (requirement 6).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import MonitorSource, SourceName, Topic, TopicStatus


class FilterUnknown(ValueError):
    """The named topic or source does not exist for this monitor."""


async def resolve_topic(
    session: AsyncSession, *, monitor_id: uuid.UUID, topic_id: uuid.UUID
) -> uuid.UUID:
    """Follows ``merged_into`` to the surviving identifier. Raises
    ``FilterUnknown`` if the topic does not belong to this monitor at all."""
    topic = await session.get(Topic, topic_id)
    if topic is None or topic.monitor_id != monitor_id:
        raise FilterUnknown(f"topic {topic_id} is not known to monitor {monitor_id}")

    seen = {topic.id}
    while topic.status is TopicStatus.MERGED and topic.merged_into is not None:
        if topic.merged_into in seen:
            break  # a lineage cycle would be a data bug elsewhere; do not loop forever
        topic = await session.get(Topic, topic.merged_into)
        if topic is None:
            break
        seen.add(topic.id)

    return topic.id


async def resolve_source(
    session: AsyncSession, *, monitor_id: uuid.UUID, source_name: SourceName
) -> SourceName:
    """Raises ``FilterUnknown`` if this source is not attached to the monitor."""
    result = await session.execute(
        select(MonitorSource.id).where(
            MonitorSource.monitor_id == monitor_id, MonitorSource.source_name == source_name
        )
    )
    if result.first() is None:
        raise FilterUnknown(f"source {source_name.value} is not attached to monitor {monitor_id}")
    return source_name
