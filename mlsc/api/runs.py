"""Trigger and read endpoints. Trigger enqueues and returns; it never collects in-request."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from mlsc.application.runs import MonitorNotRunnable, RunAlreadyActive
from mlsc.repositories.runs import RunRepository
from mlsc.schemas.metrics import RunView

router = APIRouter(tags=["runs"])


class RunTriggerRequest(BaseModel):
    run_date: date | None = None
    is_backfill: bool = False


class RunTriggerResponse(BaseModel):
    run_id: uuid.UUID


@router.post(
    "/monitors/{monitor_id}/runs",
    response_model=RunTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_run(
    monitor_id: uuid.UUID, body: RunTriggerRequest, request: Request
) -> RunTriggerResponse:
    run_service = request.app.state.run_service
    run_date = body.run_date or date.today()
    try:
        run_id = await run_service.start(monitor_id, run_date, is_backfill=body.is_backfill)
    except MonitorNotRunnable:
        raise HTTPException(status.HTTP_409_CONFLICT, "monitor is not active") from None
    except RunAlreadyActive as active:
        return RunTriggerResponse(run_id=active.run_id)
    return RunTriggerResponse(run_id=run_id)


@router.get("/runs/{run_id}", response_model=RunView)
async def get_run(run_id: uuid.UUID, request: Request) -> RunView:
    """Requirement 5: a client polls a run id and sees the stage reached and
    whether it succeeded, was partial, or failed."""
    async with request.app.state.startup.session_factory() as session:
        run = await RunRepository(session).get(run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} not found")
    return RunView(
        id=run.id, monitor_id=run.monitor_id, run_date=run.run_date, status=run.status.value,
        stage_status=run.stage_status, started_at=run.started_at, finished_at=run.finished_at,
    )


@router.get("/monitors/{monitor_id}/runs")
async def list_runs(monitor_id: uuid.UUID, request: Request) -> list[dict]:
    async with request.app.state.startup.session_factory() as session:
        runs = await RunRepository(session).history_for_monitor(monitor_id)
    return [
        {"id": r.id, "run_date": r.run_date, "status": r.status, "is_backfill": r.is_backfill}
        for r in runs
    ]
