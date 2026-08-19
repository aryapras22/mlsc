"""Request and response contracts for attaching a source to a monitor.

Source configuration is untrusted because it was typed by a user, and is
validated once here, at attach time — not re-checked on every collection
(design.md, "Trust boundary").
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from mlsc.db.models import SourceName

_PLAY_PACKAGE_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")


def _validate_play_config(config: dict[str, Any]) -> str:
    """Check the configured package id and return it as the source's instance key."""
    package_id = config.get("package_id")
    if not isinstance(package_id, str) or not _PLAY_PACKAGE_ID_PATTERN.match(package_id):
        raise ValueError(
            f"config.package_id {package_id!r} is not a well-formed Play package identifier"
        )
    return package_id


_INSTANCE_KEY_VALIDATORS = {
    SourceName.PLAY: _validate_play_config,
}


def instance_key_for(source_name: SourceName, config: dict[str, Any]) -> str:
    """Validate ``config`` for ``source_name`` and return its instance key.

    Only Play is implemented; other source names raise until their adapter and
    validator arrive in ``multi-source-ingestion``.
    """
    validator = _INSTANCE_KEY_VALIDATORS.get(source_name)
    if validator is None:
        raise ValueError(f"source {source_name.value!r} is not yet implemented")
    return validator(config)


class MonitorSourceCreateRequest(BaseModel):
    source_name: SourceName
    config: dict[str, Any]
    daily_quota: int = Field(gt=0)
    enabled: bool = True

    @model_validator(mode="after")
    def _check_config_matches_source(self) -> MonitorSourceCreateRequest:
        instance_key_for(self.source_name, self.config)
        return self


class MonitorSourceUpdateRequest(BaseModel):
    daily_quota: int | None = Field(default=None, gt=0)
    enabled: bool | None = None


class MonitorSourceResponse(BaseModel):
    id: uuid.UUID
    monitor_id: uuid.UUID
    source_name: SourceName
    instance_key: str
    config: dict[str, Any]
    daily_quota: int
    enabled: bool
    last_external_id: str | None
    last_published_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
