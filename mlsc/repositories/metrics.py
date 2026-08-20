"""Persistence for daily topic metrics: idempotent upsert per bucket, and the
prune that removes rows a recompute no longer supports.

Takes a caller-owned ``AsyncSession`` and never commits, matching
``mlsc/repositories/monitors.py``. Upsert targets one of four partial unique
indexes depending on which of ``source_name``/``topic_id`` is the all-value
``None`` for that row — a plain unique constraint could not do this, because
SQL never considers two NULLs equal (design.md, "Domain shapes": ``MetricKey``'s
all-value admission).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import DailyMetric, RollupReason
from mlsc.pipeline.analytics.group import GroupedFigures

_UPDATE_COLUMNS = (
    "doc_count",
    "doc_count_share",
    "sample_size",
    "quota_hit",
    "sentiment_mean",
    "sentiment_p25",
    "negativity_rate",
    "engagement_sum",
    "author_diversity",
    "rating_mean",
    "intent_counts",
    "reason",
)


class MetricRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_bucket(
        self,
        *,
        monitor_id: uuid.UUID,
        bucket: date,
        rows: list[GroupedFigures],
        reason: RollupReason,
    ) -> None:
        """Write every row for one bucket, converging on the same figures
        when run twice (requirement 8) rather than duplicating or drifting.
        """
        for row in rows:
            await self._upsert_one(monitor_id=monitor_id, bucket=bucket, row=row, reason=reason)

    async def _upsert_one(
        self,
        *,
        monitor_id: uuid.UUID,
        bucket: date,
        row: GroupedFigures,
        reason: RollupReason,
    ) -> None:
        values = {
            "id": uuid.uuid4(),
            "monitor_id": monitor_id,
            "bucket": bucket,
            "source_name": row.source_name,
            "topic_id": row.topic_id,
            "doc_count": row.figures.doc_count,
            "doc_count_share": row.figures.doc_count_share,
            "sample_size": row.sample_size,
            "quota_hit": row.quota_hit,
            "sentiment_mean": row.figures.sentiment_mean,
            "sentiment_p25": row.figures.sentiment_p25,
            "negativity_rate": row.figures.negativity_rate,
            "engagement_sum": row.figures.engagement_sum,
            "author_diversity": row.figures.author_diversity,
            "rating_mean": row.figures.rating_mean,
            "intent_counts": row.figures.intent_counts,
            "reason": reason,
        }

        index_elements, index_where = _conflict_target(row.source_name, row.topic_id)
        statement = insert(DailyMetric).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=index_elements,
            index_where=index_where,
            set_={column: getattr(statement.excluded, column) for column in _UPDATE_COLUMNS},
        )
        await self._session.execute(statement)

    async def prune_unsupported(
        self,
        *,
        monitor_id: uuid.UUID,
        bucket: date,
        surviving_keys: set[tuple[str | None, uuid.UUID | None]],
    ) -> int:
        """Delete rows for ``bucket`` whose (source, topic) key is not among
        ``surviving_keys`` — the recompute's own output.

        This is what makes recomputation idempotent under change: a topic
        merged away or a document purged by retention shrinks the group set,
        and the leftover row from the previous computation is a stale
        figure no query can explain unless it is removed here (design.md,
        "Success path": "the prune step").
        """
        result = await self._session.execute(
            select(DailyMetric.id, DailyMetric.source_name, DailyMetric.topic_id).where(
                DailyMetric.monitor_id == monitor_id, DailyMetric.bucket == bucket
            )
        )
        existing = result.all()

        stale_ids = [
            row_id
            for row_id, source_name, topic_id in existing
            if (source_name.value if source_name else None, topic_id) not in surviving_keys
        ]
        if not stale_ids:
            return 0

        await self._session.execute(delete(DailyMetric).where(DailyMetric.id.in_(stale_ids)))
        return len(stale_ids)

    async def for_topic(self, topic_id: uuid.UUID) -> list[DailyMetric]:
        """Read every metric row addressed to ``topic_id`` directly.

        Lineage-aware in the sense requirement 7 needs: a metric row is
        never rewritten when a topic merges (`persistent-topics` moves
        assignments, not metric history), so a row recorded against the
        absorbed identifier stays exactly here, findable by that identifier,
        for as long as the topic row itself is not deleted — which C3
        guarantees it never is.
        """
        result = await self._session.execute(
            select(DailyMetric).where(DailyMetric.topic_id == topic_id).order_by(DailyMetric.bucket)
        )
        return list(result.scalars().all())


def _conflict_target(
    source_name: object, topic_id: uuid.UUID | None
) -> tuple[list[str], object]:
    """Which partial unique index this row's key belongs to, matching the
    four indexes the migration created."""
    from mlsc.db.models import DailyMetric as M

    if source_name is not None and topic_id is not None:
        return (
            ["monitor_id", "bucket", "source_name", "topic_id"],
            and_(M.source_name.is_not(None), M.topic_id.is_not(None)),
        )
    if source_name is not None and topic_id is None:
        return (
            ["monitor_id", "bucket", "source_name"],
            and_(M.source_name.is_not(None), M.topic_id.is_(None)),
        )
    if source_name is None and topic_id is not None:
        return (
            ["monitor_id", "bucket", "topic_id"],
            and_(M.source_name.is_(None), M.topic_id.is_not(None)),
        )
    return (
        ["monitor_id", "bucket"],
        and_(M.source_name.is_(None), M.topic_id.is_(None)),
    )
