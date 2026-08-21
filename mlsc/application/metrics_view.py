"""Builds every read-only metric view over one monitor and range.

Every function here returns a schema with its ``data_quality`` already
attached — there is no code path that returns a ``Series`` or ``Overview``
without one (design.md, "Failure strategy": "Quality assembly failure —
crash"). This is what makes requirement 2 structural rather than a
convention each endpoint has to remember (learn.md, "The data-quality block
belongs to the wrapper, not the endpoint").
"""

from __future__ import annotations

import statistics
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.application import quality as quality_module
from mlsc.db.models import DailyMetric, SourceName, Topic, TrendScore
from mlsc.schemas.metrics import (
    Absence,
    DateRange,
    Metric,
    Overview,
    PointQuality,
    Series,
    SeriesPoint,
    TopicRanking,
    TopicRankingEntry,
)

_METRIC_COLUMNS: dict[Metric, str] = {
    Metric.VOLUME: "doc_count",
    Metric.PREVALENCE: "doc_count_share",
    Metric.SENTIMENT: "sentiment_mean",
    Metric.ENGAGEMENT: "engagement_sum",
    Metric.AUTHOR_DIVERSITY: "author_diversity",
    Metric.RATING: "rating_mean",
}


async def build_series(
    session: AsyncSession,
    *,
    monitor_id: uuid.UUID,
    metric: Metric,
    start: date,
    end: date,
    topic_id: uuid.UUID | None,
    source: SourceName | None,
) -> Series:
    result = await session.execute(
        select(DailyMetric).where(
            DailyMetric.monitor_id == monitor_id, DailyMetric.topic_id == topic_id,
            DailyMetric.source_name == source, DailyMetric.bucket >= start, DailyMetric.bucket <= end,
        ).order_by(DailyMetric.bucket)
    )
    rows = list(result.scalars().all())
    column = _METRIC_COLUMNS[metric]

    points = [
        SeriesPoint(bucket=row.bucket, value=getattr(row, column), quality=_point_quality(row))
        for row in rows
    ]

    data_quality = await quality_module.assemble(session, monitor_id=monitor_id, start=start, end=end)
    absence = Absence.NO_DATA if not points else None
    return Series(
        metric=metric, topic_id=topic_id, source=source, points=points,
        absence=absence, data_quality=data_quality,
    )


async def build_overview(session: AsyncSession, *, monitor_id: uuid.UUID, start: date, end: date) -> Overview:
    result = await session.execute(
        select(DailyMetric).where(
            DailyMetric.monitor_id == monitor_id, DailyMetric.source_name.is_(None),
            DailyMetric.topic_id.is_(None), DailyMetric.bucket >= start, DailyMetric.bucket <= end,
        )
    )
    rows = list(result.scalars().all())

    sentiment_values = [row.sentiment_mean for row in rows if row.sentiment_mean is not None]
    headline = {
        "doc_count": float(sum(row.doc_count for row in rows)) if rows else None,
        "sentiment_mean": statistics.fmean(sentiment_values) if sentiment_values else None,
    }

    data_quality = await quality_module.assemble(session, monitor_id=monitor_id, start=start, end=end)
    return Overview(period=_range(start, end), headline_figures=headline, data_quality=data_quality)


async def build_ranking(session: AsyncSession, *, monitor_id: uuid.UUID, start: date, end: date) -> TopicRanking:
    metric_result = await session.execute(
        select(DailyMetric).where(
            DailyMetric.monitor_id == monitor_id, DailyMetric.source_name.is_(None),
            DailyMetric.topic_id.is_not(None), DailyMetric.bucket >= start, DailyMetric.bucket <= end,
        )
    )
    rows_by_topic: dict[uuid.UUID, list[DailyMetric]] = {}
    for row in metric_result.scalars().all():
        rows_by_topic.setdefault(row.topic_id, []).append(row)

    entries: list[TopicRankingEntry] = []
    for topic_id, rows in rows_by_topic.items():
        topic = await session.get(Topic, topic_id)
        if topic is None:
            continue
        sentiment_values = [row.sentiment_mean for row in rows if row.sentiment_mean is not None]
        score_result = await session.execute(
            select(TrendScore.value).where(
                TrendScore.monitor_id == monitor_id, TrendScore.topic_id == topic_id,
                TrendScore.bucket >= start, TrendScore.bucket <= end, TrendScore.withheld_reason.is_(None),
            ).order_by(TrendScore.bucket.desc()).limit(1)
        )
        entries.append(
            TopicRankingEntry(
                topic_id=topic_id, label=topic.label, doc_count=sum(row.doc_count for row in rows),
                doc_count_share=statistics.fmean(row.doc_count_share for row in rows),
                sentiment_mean=statistics.fmean(sentiment_values) if sentiment_values else None,
                trend_score=score_result.scalar_one_or_none(),
            )
        )
    entries.sort(key=lambda entry: entry.doc_count, reverse=True)

    data_quality = await quality_module.assemble(session, monitor_id=monitor_id, start=start, end=end)
    return TopicRanking(entries=entries, data_quality=data_quality)


def _point_quality(row: DailyMetric) -> PointQuality:
    if row.quota_hit:
        return PointQuality.TRUNCATED
    if row.doc_count == 0:
        return PointQuality.PARTIAL
    return PointQuality.CLEAN


def _range(start: date, end: date) -> DateRange:
    return DateRange(start=start, end=end)
