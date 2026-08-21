"""Assembles ``TopicContext``: the one inspectable value a prompt sees.

The generator never reaches into the database — every fact it can cite must
already be in this value, which is what makes "what grounded this insight"
a question a test can answer directly rather than by re-running a query
(design.md, "Success path": "That boundary is deliberate").
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date

from sqlalchemy import select as sql_select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import DailyMetric, Topic, TrendScore
from mlsc.pipeline.insights.representatives import Representative
from mlsc.pipeline.insights.representatives import select as select_representatives

_REPRESENTATIVE_LIMIT = 15


class ContextEmpty(RuntimeError):
    """The topic has no representative documents in the period (design.md,
    "Named failures")."""


@dataclasses.dataclass(frozen=True)
class TopicStatistics:
    doc_count: int
    doc_count_share: float
    sentiment_mean: float | None
    negativity_rate: float | None
    breadth_ratio: float
    trend_score: float | None
    intent_mix: dict[str, float]


@dataclasses.dataclass(frozen=True)
class TopicContext:
    topic_id: uuid.UUID
    topic_label: str
    period_start: date
    period_end: date
    representatives: list[Representative]
    statistics: TopicStatistics


async def assemble(
    session: AsyncSession, *, topic: Topic, period_start: date, period_end: date
) -> TopicContext:
    """Raises ``ContextEmpty`` when the topic has no representatives —
    the caller records that as a skip rather than assembling half a
    context."""
    representatives = await select_representatives(
        session, topic=topic, period_start=period_start, period_end=period_end,
        limit=_REPRESENTATIVE_LIMIT,
    )
    if not representatives:
        raise ContextEmpty(f"topic {topic.id} has no representatives in {period_start}..{period_end}")

    statistics = await _load_statistics(
        session, monitor_id=topic.monitor_id, topic_id=topic.id,
        period_start=period_start, period_end=period_end,
    )

    return TopicContext(
        topic_id=topic.id, topic_label=topic.label, period_start=period_start, period_end=period_end,
        representatives=representatives, statistics=statistics,
    )


async def _load_statistics(
    session: AsyncSession, *, monitor_id: uuid.UUID, topic_id: uuid.UUID, period_start: date, period_end: date
) -> TopicStatistics:
    metrics_result = await session.execute(
        sql_select(DailyMetric).where(
            DailyMetric.monitor_id == monitor_id, DailyMetric.topic_id == topic_id,
            DailyMetric.source_name.is_(None),
            DailyMetric.bucket >= period_start, DailyMetric.bucket < period_end,
        )
    )
    topic_metrics = list(metrics_result.scalars().all())

    doc_count = sum(metric.doc_count for metric in topic_metrics)
    doc_count_share = _mean([metric.doc_count_share for metric in topic_metrics])
    sentiment_mean = _mean([m.sentiment_mean for m in topic_metrics if m.sentiment_mean is not None])
    negativity_rate = _mean([m.negativity_rate for m in topic_metrics if m.negativity_rate is not None])

    intent_totals: dict[str, int] = {}
    for metric in topic_metrics:
        for intent, count in metric.intent_counts.items():
            intent_totals[intent] = intent_totals.get(intent, 0) + count
    intent_mix = (
        {intent: count / doc_count for intent, count in intent_totals.items()} if doc_count else {}
    )

    breadth_ratio = await _breadth_ratio(
        session, monitor_id=monitor_id, topic_id=topic_id, period_start=period_start, period_end=period_end,
    )

    score_result = await session.execute(
        sql_select(TrendScore.value).where(
            TrendScore.monitor_id == monitor_id, TrendScore.topic_id == topic_id,
            TrendScore.bucket >= period_start, TrendScore.bucket < period_end,
            TrendScore.withheld_reason.is_(None),
        ).order_by(TrendScore.bucket.desc()).limit(1)
    )
    trend_score = score_result.scalar_one_or_none()

    return TopicStatistics(
        doc_count=doc_count, doc_count_share=doc_count_share, sentiment_mean=sentiment_mean,
        negativity_rate=negativity_rate, breadth_ratio=breadth_ratio, trend_score=trend_score,
        intent_mix=intent_mix,
    )


async def _breadth_ratio(
    session: AsyncSession, *, monitor_id: uuid.UUID, topic_id: uuid.UUID, period_start: date, period_end: date
) -> float:
    active_result = await session.execute(
        sql_select(DailyMetric.source_name).where(
            DailyMetric.monitor_id == monitor_id, DailyMetric.topic_id.is_(None),
            DailyMetric.source_name.is_not(None),
            DailyMetric.bucket >= period_start, DailyMetric.bucket < period_end,
        ).distinct()
    )
    active_sources = {row[0] for row in active_result.all()}
    if not active_sources:
        return 0.0

    with_topic_result = await session.execute(
        sql_select(DailyMetric.source_name).where(
            DailyMetric.monitor_id == monitor_id, DailyMetric.topic_id == topic_id,
            DailyMetric.source_name.is_not(None),
            DailyMetric.bucket >= period_start, DailyMetric.bucket < period_end,
        ).distinct()
    )
    sources_with_topic = {row[0] for row in with_topic_result.all()}

    return len(sources_with_topic) / len(active_sources)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
