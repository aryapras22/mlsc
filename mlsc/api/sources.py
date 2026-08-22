"""Thin router: validates via schemas, invokes MonitorSourceService, maps named failures."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status

from mlsc.application.sources import MonitorSourceService
from mlsc.repositories.sources import MonitorSourceNotFound
from mlsc.schemas.sources import (
    MonitorSourceCreateRequest,
    MonitorSourceResponse,
    MonitorSourceUpdateRequest,
)

router = APIRouter(tags=["sources"])


def _service(request: Request) -> MonitorSourceService:
    return request.app.state.monitor_source_service


@router.post(
    "/monitors/{monitor_id}/sources",
    response_model=MonitorSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def attach_source(
    monitor_id: uuid.UUID, body: MonitorSourceCreateRequest, request: Request
) -> MonitorSourceResponse:
    return await _service(request).attach(monitor_id, body)


@router.get("/monitors/{monitor_id}/sources", response_model=list[MonitorSourceResponse])
async def list_sources(monitor_id: uuid.UUID, request: Request) -> list[MonitorSourceResponse]:
    return await _service(request).list_for_monitor(monitor_id)


@router.patch("/sources/{source_id}", response_model=MonitorSourceResponse)
async def update_source(
    source_id: uuid.UUID, body: MonitorSourceUpdateRequest, request: Request
) -> MonitorSourceResponse:
    try:
        return await _service(request).update(source_id, body)
    except MonitorSourceNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"source {source_id} not found") from None
