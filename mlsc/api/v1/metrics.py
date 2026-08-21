"""Overview, timeseries, and topic ranking — the three read shapes every
request follows the same four steps for: validate, scope, resolve filters,
attach quality (design.md, "Success path") — which is what makes
requirement 2 structural rather than a habit.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import ValidationError

from mlsc.api.scoping import Scoping
from mlsc.application import metrics_view
from mlsc.application.filters import FilterUnknown, resolve_source, resolve_topic
from mlsc.db.models import SourceName
from mlsc.repositories.monitors import MonitorNotFound
from mlsc.schemas.metrics import DateRange, EntityComparison, Metric, Overview, Series, TopicRanking

router = APIRouter(prefix="/monitors/{monitor_id}", tags=["metrics"])


def _validate_range(start: date, end: date) -> DateRange:
    try:
        return DateRange(start=start, end=end)
    except ValidationError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from None


@router.get("/overview", response_model=Overview)
async def get_overview(monitor_id: uuid.UUID, request: Request, start: date, end: date) -> Overview:
    date_range = _validate_range(start, end)
    async with request.app.state.startup.session_factory() as session:
        try:
            await Scoping(session).resolve(monitor_id)
        except MonitorNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} not found") from None
        return await metrics_view.build_overview(
            session, monitor_id=monitor_id, start=date_range.start, end=date_range.end
        )


@router.get("/timeseries", response_model=Series)
async def get_timeseries(
    monitor_id: uuid.UUID,
    request: Request,
    metric: Metric,
    start: date,
    end: date,
    topic_id: uuid.UUID | None = Query(default=None),
    source: SourceName | None = Query(default=None),
) -> Series:
    date_range = _validate_range(start, end)
    async with request.app.state.startup.session_factory() as session:
        try:
            await Scoping(session).resolve(monitor_id)
        except MonitorNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} not found") from None

        try:
            resolved_topic_id = (
                await resolve_topic(session, monitor_id=monitor_id, topic_id=topic_id)
                if topic_id is not None else None
            )
            resolved_source = (
                await resolve_source(session, monitor_id=monitor_id, source_name=source)
                if source is not None else None
            )
        except FilterUnknown as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from None

        return await metrics_view.build_series(
            session, monitor_id=monitor_id, metric=metric, start=date_range.start, end=date_range.end,
            topic_id=resolved_topic_id, source=resolved_source,
        )


@router.get("/topics/ranking", response_model=TopicRanking)
async def get_topic_ranking(monitor_id: uuid.UUID, request: Request, start: date, end: date) -> TopicRanking:
    date_range = _validate_range(start, end)
    async with request.app.state.startup.session_factory() as session:
        try:
            await Scoping(session).resolve(monitor_id)
        except MonitorNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} not found") from None
        return await metrics_view.build_ranking(
            session, monitor_id=monitor_id, start=date_range.start, end=date_range.end
        )


@router.get("/compare", response_model=EntityComparison)
async def get_entity_comparison(monitor_id: uuid.UUID, request: Request, start: date, end: date) -> EntityComparison:
    date_range = _validate_range(start, end)
    async with request.app.state.startup.session_factory() as session:
        try:
            await Scoping(session).resolve(monitor_id)
        except MonitorNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} not found") from None
        return await metrics_view.build_comparison(
            session, monitor_id=monitor_id, start=date_range.start, end=date_range.end
        )
