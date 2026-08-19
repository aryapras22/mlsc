"""Request and response contracts for monitors, distinct from the ORM types.

Validation here is the trust boundary (design.md, "Trust boundary"): name, cron
expression, timezone, and seed shape are all checked once, before the service
is ever called. Nothing downstream re-checks.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal
from zoneinfo import available_timezones

from croniter import croniter
from pydantic import BaseModel, Field, field_validator, model_validator

from mlsc.db.models import MonitorStatus, TargetType

_CRON_FIELD_COUNT = 5
_AVAILABLE_TIMEZONES = available_timezones()


def _validate_name(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    return stripped


def _validate_cron_expression(value: str) -> str:
    if len(value.split()) != _CRON_FIELD_COUNT or not croniter.is_valid(value):
        raise ValueError(f"cron_expression {value!r} is not a valid five-field cron expression")
    return value


def _validate_timezone(value: str) -> str:
    if value not in _AVAILABLE_TIMEZONES:
        raise ValueError(f"timezone {value!r} is not a recognised IANA timezone")
    return value


def validate_seed(target_type: TargetType, seed: dict[str, Any]) -> None:
    """Check seed shape against its target type.

    Exposed for reuse by ``MonitorService.update``: an update request carries no
    ``target_type`` (it is immutable), so a seed change can only be checked once
    the existing monitor's target type is loaded.
    """
    if target_type is TargetType.PRODUCT:
        identifiers = seed.get("identifiers")
        if not isinstance(identifiers, list) or not identifiers:
            raise ValueError("seed.identifiers must be a non-empty list for a product monitor")
    else:
        description = seed.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("seed.description must be a non-empty string for a theme monitor")


class MonitorCreateRequest(BaseModel):
    name: str
    target_type: TargetType
    seed: dict[str, Any]
    cron_expression: str
    timezone: str
    retention_days: int = Field(gt=0)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _validate_name(value)

    @field_validator("cron_expression")
    @classmethod
    def _check_cron_expression(cls, value: str) -> str:
        return _validate_cron_expression(value)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        return _validate_timezone(value)

    @model_validator(mode="after")
    def _check_seed_matches_target_type(self) -> MonitorCreateRequest:
        validate_seed(self.target_type, self.seed)
        return self


class MonitorUpdateRequest(BaseModel):
    name: str | None = None
    seed: dict[str, Any] | None = None
    cron_expression: str | None = None
    timezone: str | None = None
    retention_days: int | None = Field(default=None, gt=0)
    status: Literal[MonitorStatus.ACTIVE, MonitorStatus.PAUSED, MonitorStatus.ARCHIVED] | None = (
        None
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        return _validate_name(value) if value is not None else value

    @field_validator("cron_expression")
    @classmethod
    def _check_cron_expression(cls, value: str | None) -> str | None:
        return _validate_cron_expression(value) if value is not None else value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        return _validate_timezone(value) if value is not None else value


class MonitorResponse(BaseModel):
    id: uuid.UUID
    name: str
    target_type: TargetType
    seed: dict[str, Any]
    cron_expression: str
    timezone: str
    status: MonitorStatus
    retention_days: int
    created_at: datetime
    projected: bool

    model_config = {"from_attributes": True}

    @classmethod
    def from_monitor(cls, monitor: Any) -> MonitorResponse:
        return cls(
            id=monitor.id,
            name=monitor.name,
            target_type=monitor.target_type,
            seed=monitor.seed,
            cron_expression=monitor.schedule,
            timezone=monitor.timezone,
            status=monitor.status,
            retention_days=monitor.retention_days,
            created_at=monitor.created_at,
            projected=monitor.registration is not None,
        )
