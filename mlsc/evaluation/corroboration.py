"""The corroboration test: whether an event corroborated by several
sources matches a labelled event more often than one seen in a single
source (requirement 6) — the paper's complementarity claim, tested rather
than assumed.

Breadth partitions on the same signal `trend-detection`'s composite score
already reads: the proportion of active sources reporting the topic that
day (design.md's ``breadth_ratio``). ``TrendEvent`` carries no breadth
field of its own, so it is read from the same-day ``TrendScore`` row's
``components["breadth_ratio"]`` the production scorer already wrote.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import EventLabel, TrendEvent, TrendScore
from mlsc.evaluation.detection import match_events
from mlsc.evaluation.measures import Measure, computed, unavailable_no_labels

_BREADTH_SPLIT = 0.5


async def measure_corroboration(
    session: AsyncSession, *, monitor_id: uuid.UUID, labels: list[EventLabel], tolerance_days: int = 3
) -> tuple[Measure, Measure]:
    """Requirement 6: partition this monitor's events by whether their
    day's breadth ratio for their topic is above or below ``_BREADTH_SPLIT``,
    then compare each partition's hit rate against ``labels`` — returns
    (corroborated_hit_rate, single_source_hit_rate)."""
    if not labels:
        return (
            unavailable_no_labels("corroboration_hit_rate_corroborated", computed_over="0 event labels"),
            unavailable_no_labels("corroboration_hit_rate_single_source", computed_over="0 event labels"),
        )

    events_result = await session.execute(select(TrendEvent).where(TrendEvent.monitor_id == monitor_id))
    events = list(events_result.scalars().all())

    scores_result = await session.execute(select(TrendScore).where(TrendScore.monitor_id == monitor_id))
    breadth_by_topic_bucket: dict[tuple[uuid.UUID, object], float] = {
        (score.topic_id, score.bucket): (score.components or {}).get("breadth_ratio", 0.0)
        for score in scores_result.scalars().all()
    }

    corroborated: list[TrendEvent] = []
    single_source: list[TrendEvent] = []
    for event in events:
        breadth = breadth_by_topic_bucket.get((event.topic_id, event.detected_on))
        if breadth is None:
            continue  # no same-day score row; breadth is unknown, exclude rather than guess
        (corroborated if breadth >= _BREADTH_SPLIT else single_source).append(event)

    corroborated_matches = match_events(corroborated, labels, tolerance_days=tolerance_days)
    single_source_matches = match_events(single_source, labels, tolerance_days=tolerance_days)

    corroborated_rate = len(corroborated_matches) / len(corroborated) if corroborated else 0.0
    single_source_rate = len(single_source_matches) / len(single_source) if single_source else 0.0

    return (
        computed(
            "corroboration_hit_rate_corroborated", corroborated_rate, sample_size=len(corroborated),
            computed_over=f"{len(corroborated)} multi-source events against {len(labels)} labels",
        ),
        computed(
            "corroboration_hit_rate_single_source", single_source_rate, sample_size=len(single_source),
            computed_over=f"{len(single_source)} single-source events against {len(labels)} labels",
        ),
    )
