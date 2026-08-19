"""Thin router: validates via schemas, invokes MonitorService, maps named failures."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status

from mlsc.application.monitors import MonitorService
from mlsc.repositories.monitors import MonitorNotFound
from mlsc.schemas.monitors import MonitorCreateRequest, MonitorResponse, MonitorUpdateRequest

router = APIRouter(prefix="/monitors", tags=["monitors"])


def _service(request: Request) -> MonitorService:
    return request.app.state.monitor_service


@router.post("", response_model=MonitorResponse, status_code=status.HTTP_201_CREATED)
async def create_monitor(body: MonitorCreateRequest, request: Request) -> MonitorResponse:
    return await _service(request).create(body)


@router.get("", response_model=list[MonitorResponse])
async def list_monitors(request: Request) -> list[MonitorResponse]:
    return await _service(request).list_all()


@router.get("/{monitor_id}", response_model=MonitorResponse)
async def get_monitor(monitor_id: uuid.UUID, request: Request) -> MonitorResponse:
    try:
        return await _service(request).get(monitor_id)
    except MonitorNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} not found") from None


@router.patch("/{monitor_id}", response_model=MonitorResponse)
async def update_monitor(
    monitor_id: uuid.UUID, body: MonitorUpdateRequest, request: Request
) -> MonitorResponse:
    try:
        return await _service(request).update(monitor_id, body)
    except MonitorNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} not found") from None
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from None
