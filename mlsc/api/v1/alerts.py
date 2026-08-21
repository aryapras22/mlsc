"""Alert rule management: create and list. Evaluation and delivery are a
task, not a request (design.md, "Success path": "Alert evaluation is a
task, not part of a request")."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status

from mlsc.application.alerts import AlertRuleService, RuleConflict
from mlsc.schemas.alerts import AlertRuleCreateRequest, AlertRuleResponse

router = APIRouter(prefix="/monitors/{monitor_id}/alert-rules", tags=["alerts"])


def _service(request: Request) -> AlertRuleService:
    return AlertRuleService(request.app.state.startup.session_factory)


@router.post("", response_model=AlertRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_alert_rule(
    monitor_id: uuid.UUID, body: AlertRuleCreateRequest, request: Request
) -> AlertRuleResponse:
    try:
        rule = await _service(request).create(monitor_id, body)
    except RuleConflict as error:
        raise HTTPException(status.HTTP_409_CONFLICT, f"an equivalent rule already exists: {error}") from None
    return AlertRuleResponse.model_validate(rule)


@router.get("", response_model=list[AlertRuleResponse])
async def list_alert_rules(monitor_id: uuid.UUID, request: Request) -> list[AlertRuleResponse]:
    rules = await _service(request).list_for_monitor(monitor_id)
    return [AlertRuleResponse.model_validate(rule) for rule in rules]
