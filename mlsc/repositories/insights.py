"""Persistence for insights and generation skips.

Takes a caller-owned ``AsyncSession`` and never commits, matching every
other repository in this codebase.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import GenerationSkip, Insight, InsightKind, SkipReason

_OPPORTUNITY_UPDATE_COLUMNS = (
    "title", "body", "who", "what", "why", "score", "score_components",
    "evidence_ids", "llm_provider", "llm_model", "prompt_version",
)
_DIGEST_UPDATE_COLUMNS = ("title", "body", "evidence_ids", "llm_provider", "llm_model", "prompt_version")


class InsightRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_opportunity(
        self,
        *,
        monitor_id: uuid.UUID,
        topic_id: uuid.UUID,
        period_start: date,
        period_end: date,
        title: str,
        body: str,
        who: str,
        what: str,
        why: str,
        score: float,
        score_components: dict,
        evidence_ids: list[str],
        llm_provider: str,
        llm_model: str,
        prompt_version: str,
    ) -> uuid.UUID:
        """Requirement 8/C12: a re-run of the same period with no new
        evidence converges on this row rather than duplicating it."""
        values = {
            "id": uuid.uuid4(), "monitor_id": monitor_id, "topic_id": topic_id,
            "period_start": period_start, "period_end": period_end, "kind": InsightKind.OPPORTUNITY,
            "title": title, "body": body, "who": who, "what": what, "why": why,
            "score": score, "score_components": score_components, "evidence_ids": evidence_ids,
            "llm_provider": llm_provider, "llm_model": llm_model, "prompt_version": prompt_version,
        }
        statement = insert(Insight).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["monitor_id", "topic_id", "period_start", "period_end", "kind"],
            index_where=Insight.topic_id.is_not(None),
            set_={column: getattr(statement.excluded, column) for column in _OPPORTUNITY_UPDATE_COLUMNS},
        ).returning(Insight.id)
        result = await self._session.execute(statement)
        return result.scalar_one()

    async def upsert_digest(
        self,
        *,
        monitor_id: uuid.UUID,
        period_start: date,
        period_end: date,
        title: str,
        body: str,
        evidence_ids: list[str],
        llm_provider: str,
        llm_model: str,
        prompt_version: str,
    ) -> uuid.UUID:
        """A digest has no single topic (design.md, "Domain shapes"), so
        this upserts against the all-topics partial index instead."""
        values = {
            "id": uuid.uuid4(), "monitor_id": monitor_id, "topic_id": None,
            "period_start": period_start, "period_end": period_end, "kind": InsightKind.DIGEST,
            "title": title, "body": body, "who": None, "what": None, "why": None,
            "score": None, "score_components": {}, "evidence_ids": evidence_ids,
            "llm_provider": llm_provider, "llm_model": llm_model, "prompt_version": prompt_version,
        }
        statement = insert(Insight).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["monitor_id", "period_start", "period_end", "kind"],
            index_where=Insight.topic_id.is_(None),
            set_={column: getattr(statement.excluded, column) for column in _DIGEST_UPDATE_COLUMNS},
        ).returning(Insight.id)
        result = await self._session.execute(statement)
        return result.scalar_one()

    async def list_for_period(
        self, monitor_id: uuid.UUID, *, period_start: date, period_end: date, kind: InsightKind
    ) -> list[Insight]:
        result = await self._session.execute(
            select(Insight).where(
                Insight.monitor_id == monitor_id, Insight.period_start == period_start,
                Insight.period_end == period_end, Insight.kind == kind,
            )
        )
        return list(result.scalars().all())

    async def get(self, insight_id: uuid.UUID) -> Insight | None:
        return await self._session.get(Insight, insight_id)


class GenerationSkipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def write(
        self, *, monitor_id: uuid.UUID, topic_id: uuid.UUID, period_start: date, period_end: date,
        reason: SkipReason,
    ) -> None:
        self._session.add(
            GenerationSkip(
                id=uuid.uuid4(), monitor_id=monitor_id, topic_id=topic_id,
                period_start=period_start, period_end=period_end, reason=reason,
            )
        )
