"""Requirement 7's refusal rules: an untrustworthy day or evidence too thin
means declining to generate, not generating with a caveat (learn.md,
"Declining beats caveating").

Each rule returns a recorded skip reason rather than raising, matching
`trend-detection`'s gates — the ordinary state of a quiet topic must not
look like an error.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import IngestionRun, RunStatus, SkipReason, TrendScore

_MIN_REPRESENTATIVES = 3


class GatingOutcome:
    __slots__ = ("topic_id", "passed", "reason")

    def __init__(self, topic_id: uuid.UUID, *, passed: bool, reason: SkipReason | None = None) -> None:
        self.topic_id = topic_id
        self.passed = passed
        self.reason = reason


async def day_is_trustworthy(
    session: AsyncSession, *, monitor_id: uuid.UUID, period_start: date, period_end: date
) -> bool:
    """C5/C6: the same partial-run signal `trend-detection` treats as
    disqualifying a baseline disqualifies generation over that period too —
    a silently broken scraper must not get a fluent explanation."""
    result = await session.execute(
        select(IngestionRun.status).where(
            IngestionRun.monitor_id == monitor_id,
            IngestionRun.run_date >= period_start,
            IngestionRun.run_date < period_end,
        )
    )
    statuses = [row[0] for row in result.all()]
    return all(status != RunStatus.PARTIAL for status in statuses)


async def has_gated_change(
    session: AsyncSession, *, monitor_id: uuid.UUID, topic_id: uuid.UUID, period_start: date, period_end: date
) -> bool:
    """C6: momentum only counts a change `trend-detection` already gated —
    a topic with no surviving score in the period has nothing an insight
    would be explaining."""
    result = await session.execute(
        select(TrendScore.id).where(
            TrendScore.monitor_id == monitor_id, TrendScore.topic_id == topic_id,
            TrendScore.bucket >= period_start, TrendScore.bucket < period_end,
            TrendScore.withheld_reason.is_(None),
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


def evidence_is_thin(representative_count: int) -> bool:
    """Requirement 7: how thin is too thin, an open decision this spec
    starts with a concrete floor for — fewer than three representatives
    means the model would be reasoning from a source's raw handful of
    documents rather than a topic's discussion."""
    return representative_count < _MIN_REPRESENTATIVES


def gate_topic(
    topic_id: uuid.UUID, *, day_trustworthy: bool, representative_count: int, has_change: bool
) -> GatingOutcome:
    if not day_trustworthy:
        return GatingOutcome(topic_id, passed=False, reason=SkipReason.DAY_UNTRUSTWORTHY)
    if evidence_is_thin(representative_count):
        return GatingOutcome(topic_id, passed=False, reason=SkipReason.EVIDENCE_TOO_THIN)
    if not has_change:
        return GatingOutcome(topic_id, passed=False, reason=SkipReason.NO_CHANGE_DETECTED)
    return GatingOutcome(topic_id, passed=True)
