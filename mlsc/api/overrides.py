"""Thin router: validates via schemas, invokes OverrideService, maps named failures."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status

from mlsc.application.overrides import OverrideOverlaps, OverrideService, PurgeNotConfirmed
from mlsc.schemas.overrides import (
    OverrideJobView,
    OverrideRequest,
    OverrideSubmitResponse,
    RetentionPreviewResponse,
)

router = APIRouter(tags=["overrides"])


def _service(request: Request) -> OverrideService:
    return request.app.state.override_service


@router.get(
    "/monitors/{monitor_id}/overrides/retention-preview", response_model=RetentionPreviewResponse
)
async def preview_retention(monitor_id: uuid.UUID, request: Request) -> RetentionPreviewResponse:
    return await _service(request).preview_retention(monitor_id)


@router.post(
    "/monitors/{monitor_id}/overrides",
    response_model=OverrideSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_override(
    monitor_id: uuid.UUID, body: OverrideRequest, request: Request
) -> OverrideSubmitResponse:
    try:
        job_id = await _service(request).submit(monitor_id, body)
    except OverrideOverlaps as overlap:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"an override of this kind is already running: {overlap.job_id}"
        ) from None
    except PurgeNotConfirmed:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "purge_token is missing or does not match the current retention preview",
        ) from None
    return OverrideSubmitResponse(job_id=job_id)


@router.get("/monitors/{monitor_id}/overrides", response_model=list[OverrideJobView])
async def list_overrides(monitor_id: uuid.UUID, request: Request) -> list[OverrideJobView]:
    jobs = await _service(request).list_for_monitor(monitor_id)
    return [OverrideJobView.model_validate(job) for job in jobs]
