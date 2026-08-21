"""Event and insight read endpoints. Each event carries its method and
statistics; each insight carries its evidence and provenance — requirement
10's promise is a field on the schema, not a client convention.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from mlsc.api.scoping import Scoping
from mlsc.application.insights import InsightNotFound, JudgementService
from mlsc.db.models import Insight, InsightKind, TrendEvent
from mlsc.repositories.monitors import MonitorNotFound
from mlsc.schemas.metrics import DateRange, EventView, InsightView

router = APIRouter(prefix="/monitors/{monitor_id}", tags=["insights"])
judgements_router = APIRouter(tags=["insights"])


class JudgementRequest(BaseModel):
    useful: bool


@router.get("/events", response_model=list[EventView])
async def list_events(monitor_id: uuid.UUID, request: Request, start: date, end: date) -> list[EventView]:
    async with request.app.state.startup.session_factory() as session:
        try:
            await Scoping(session).resolve(monitor_id)
        except MonitorNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} not found") from None

        result = await session.execute(
            select(TrendEvent).where(
                TrendEvent.monitor_id == monitor_id,
                TrendEvent.detected_on >= start, TrendEvent.detected_on <= end,
            ).order_by(TrendEvent.detected_on.desc())
        )
        return [
            EventView(
                id=event.id, topic_id=event.topic_id, detected_on=event.detected_on,
                kind=event.kind, method=event.method.value, severity=event.severity,
                statistics=event.statistics, evidence_ids=event.evidence_ids,
            )
            for event in result.scalars().all()
        ]


@router.get("/insights", response_model=list[InsightView])
async def list_insights(
    monitor_id: uuid.UUID, request: Request, start: date, end: date, kind: InsightKind | None = None
) -> list[InsightView]:
    async with request.app.state.startup.session_factory() as session:
        try:
            await Scoping(session).resolve(monitor_id)
        except MonitorNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} not found") from None

        query = select(Insight).where(
            Insight.monitor_id == monitor_id,
            Insight.period_start >= start, Insight.period_end <= end,
        )
        if kind is not None:
            query = query.where(Insight.kind == kind)
        result = await session.execute(query.order_by(Insight.period_start.desc()))
        return [_to_view(insight) for insight in result.scalars().all()]


@judgements_router.post("/insights/{insight_id}/judgement", status_code=status.HTTP_204_NO_CONTENT)
async def record_judgement(insight_id: uuid.UUID, body: JudgementRequest, request: Request) -> None:
    """Requirement 6: a user marks an opportunity useful or not, recorded
    against an existing insight — untrusted feedback validated at this
    boundary as a reference to a real row (design.md, "Trust boundary")."""
    service = JudgementService(request.app.state.startup.session_factory)
    try:
        await service.record(insight_id, useful=body.useful)
    except InsightNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"insight {insight_id} not found") from None


def _to_view(insight: Insight) -> InsightView:
    return InsightView(
        id=insight.id, monitor_id=insight.monitor_id, topic_id=insight.topic_id,
        period=DateRange(start=insight.period_start, end=insight.period_end),
        kind=insight.kind, title=insight.title, body=insight.body,
        who=insight.who, what=insight.what, why=insight.why,
        score=insight.score, score_components=insight.score_components,
        evidence_ids=insight.evidence_ids, llm_provider=insight.llm_provider,
        llm_model=insight.llm_model, prompt_version=insight.prompt_version,
    )
