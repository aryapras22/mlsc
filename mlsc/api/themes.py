"""Thin router: validates via schemas, invokes ThemeJobService and ThemeService, maps named failures."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status

from mlsc.application.themes import (
    CandidateNotViable,
    NotAThemeMonitor,
    ReviewedQuery,
    ThemeJobOverlaps,
    ThemeJobService,
    ThemeService,
)
from mlsc.db.models import CandidateState
from mlsc.repositories.monitors import MonitorNotFound
from mlsc.repositories.themes import (
    EntityCandidateNotFound,
    ThemeJobNotFound,
    ThemeJobRepository,
    ThemeSeedNotFound,
)
from mlsc.schemas.themes import (
    CandidateAcceptResponse,
    CandidateResponse,
    QuerySetResponse,
    ReviewQueriesRequest,
    ThemeJobResponse,
    ThemeJobSubmitResponse,
)

router = APIRouter(tags=["themes"])


def _jobs(request: Request) -> ThemeJobService:
    return request.app.state.theme_job_service


def _themes(request: Request) -> ThemeService:
    return request.app.state.theme_service


@router.post(
    "/monitors/{monitor_id}/theme/queries/generate",
    response_model=ThemeJobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_queries(monitor_id: uuid.UUID, request: Request) -> ThemeJobSubmitResponse:
    job_id = await _submit(_jobs(request).submit_generation, monitor_id)
    return ThemeJobSubmitResponse(job_id=job_id)


@router.post(
    "/monitors/{monitor_id}/theme/discovery",
    response_model=ThemeJobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_discovery(monitor_id: uuid.UUID, request: Request) -> ThemeJobSubmitResponse:
    job_id = await _submit(_jobs(request).submit_discovery, monitor_id)
    return ThemeJobSubmitResponse(job_id=job_id)


async def _submit(submit, monitor_id: uuid.UUID) -> uuid.UUID:
    try:
        return await submit(monitor_id)
    except MonitorNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} not found") from None
    except NotAThemeMonitor:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"monitor {monitor_id} is not a theme monitor"
        ) from None
    except ThemeJobOverlaps as overlap:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a theme job of this kind is already running: {overlap.job_id}"
        ) from None


@router.get("/monitors/{monitor_id}/theme/jobs/{job_id}", response_model=ThemeJobResponse)
async def get_theme_job(monitor_id: uuid.UUID, job_id: uuid.UUID, request: Request) -> ThemeJobResponse:
    """Requirement 1/4: the identifier handed back by generation or
    discovery has to resolve to something pollable, matching how
    ``GET /runs/{id}`` completes ``POST /monitors/{id}/runs`` (mlsc/api/runs.py)."""
    async with request.app.state.startup.session_factory() as session:
        try:
            job = await ThemeJobRepository(session).get(job_id)
        except ThemeJobNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"theme job {job_id} not found") from None
        if job.monitor_id != monitor_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"theme job {job_id} not found")
        return ThemeJobResponse.model_validate(job)


@router.get("/monitors/{monitor_id}/theme/queries", response_model=list[QuerySetResponse])
async def list_queries(monitor_id: uuid.UUID, request: Request) -> list[QuerySetResponse]:
    try:
        queries = await _themes(request).get_queries(monitor_id)
    except ThemeSeedNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} has no theme seed") from None
    return [QuerySetResponse.model_validate(query) for query in queries]


@router.put("/monitors/{monitor_id}/theme/queries", status_code=status.HTTP_204_NO_CONTENT)
async def review_queries(
    monitor_id: uuid.UUID, body: ReviewQueriesRequest, request: Request
) -> None:
    queries = [ReviewedQuery(text=item.text, rationale=item.rationale) for item in body.queries]
    try:
        await _themes(request).review_queries(monitor_id, queries)
    except ThemeSeedNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"monitor {monitor_id} has no theme seed") from None


@router.get("/monitors/{monitor_id}/theme/candidates", response_model=list[CandidateResponse])
async def list_candidates(
    monitor_id: uuid.UUID, request: Request, state: CandidateState | None = Query(default=None)
) -> list[CandidateResponse]:
    candidates = await _themes(request).list_candidates(monitor_id, state=state)
    return [CandidateResponse.model_validate(candidate) for candidate in candidates]


@router.post(
    "/monitors/{monitor_id}/theme/candidates/{candidate_id}/accept",
    response_model=CandidateAcceptResponse,
)
async def accept_candidate(
    monitor_id: uuid.UUID, candidate_id: uuid.UUID, request: Request
) -> CandidateAcceptResponse:
    try:
        source_id = await _themes(request).accept_candidate(monitor_id, candidate_id)
    except EntityCandidateNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"candidate {candidate_id} not found") from None
    except CandidateNotViable as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from None
    return CandidateAcceptResponse(source_id=source_id)


@router.post(
    "/monitors/{monitor_id}/theme/candidates/{candidate_id}/reject",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def reject_candidate(monitor_id: uuid.UUID, candidate_id: uuid.UUID, request: Request) -> None:
    try:
        await _themes(request).reject_candidate(monitor_id, candidate_id)
    except EntityCandidateNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"candidate {candidate_id} not found") from None
