"""Persistence for trend events, gate outcomes, and trend scores.

Takes a caller-owned ``AsyncSession`` and never commits, matching every
other repository in this codebase.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import EventKind, GateOutcome, TrendEvent, TrendScore

_EVENT_UPDATE_COLUMNS = ("method", "severity", "statistics", "evidence_ids")
_SCORE_UPDATE_COLUMNS = ("value", "components", "penalties", "withheld_reason")


class TrendEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_event(
        self,
        *,
        monitor_id: uuid.UUID,
        topic_id: uuid.UUID,
        detected_on: date,
        kind: EventKind,
        method: str,
        severity: float,
        statistics: dict,
        evidence_ids: list[str],
    ) -> None:
        """Requirement (C12): a re-run of the same date converges on the same
        row rather than duplicating it."""
        values = {
            "id": uuid.uuid4(),
            "monitor_id": monitor_id,
            "topic_id": topic_id,
            "detected_on": detected_on,
            "kind": kind,
            "method": method,
            "severity": severity,
            "statistics": statistics,
            "evidence_ids": evidence_ids,
        }
        statement = insert(TrendEvent).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["monitor_id", "topic_id", "detected_on", "kind"],
            set_={column: getattr(statement.excluded, column) for column in _EVENT_UPDATE_COLUMNS},
        )
        await self._session.execute(statement)

    async def last_event_dates(
        self, topic_id: uuid.UUID, *, before: date, lookback_days: int
    ) -> dict[EventKind, date]:
        """The most recent date each event kind fired for this topic, within
        ``lookback_days`` — enough history to check any cooldown window
        configured, without scanning the topic's entire lifetime."""
        window_start = before - timedelta(days=lookback_days)
        result = await self._session.execute(
            select(TrendEvent.kind, TrendEvent.detected_on)
            .where(
                TrendEvent.topic_id == topic_id,
                TrendEvent.detected_on >= window_start,
                TrendEvent.detected_on < before,
            )
            .order_by(TrendEvent.detected_on)
        )
        last_dates: dict[EventKind, date] = {}
        for kind, detected_on in result.all():
            last_dates[kind] = detected_on  # later rows overwrite, keeping the most recent
        return last_dates

    def write_gate_outcome(self, outcome: GateOutcome) -> None:
        self._session.add(outcome)

    async def events_for_bucket(self, monitor_id: uuid.UUID, bucket: date) -> list[TrendEvent]:
        result = await self._session.execute(
            select(TrendEvent).where(
                TrendEvent.monitor_id == monitor_id, TrendEvent.detected_on == bucket
            )
        )
        return list(result.scalars().all())


class TrendScoreRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_score(
        self,
        *,
        monitor_id: uuid.UUID,
        topic_id: uuid.UUID,
        bucket: date,
        value: float | None,
        components: dict,
        penalties: dict,
        withheld_reason: str | None,
    ) -> None:
        values = {
            "id": uuid.uuid4(),
            "monitor_id": monitor_id,
            "topic_id": topic_id,
            "bucket": bucket,
            "value": value,
            "components": components,
            "penalties": penalties,
            "withheld_reason": withheld_reason,
        }
        statement = insert(TrendScore).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["monitor_id", "topic_id", "bucket"],
            set_={column: getattr(statement.excluded, column) for column in _SCORE_UPDATE_COLUMNS},
        )
        await self._session.execute(statement)
