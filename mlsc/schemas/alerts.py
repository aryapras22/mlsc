"""Request and response contracts for alert rules, distinct from the ORM
model. The webhook target is validated as a URL here, at creation
(design.md, "Trust boundary") — it is treated as hostile again, later, when
it is actually called.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from mlsc.db.models import Channel, ReadAlertKind

_URL_PATTERN = re.compile(r"^https?://[^\s]+$")


class AlertRuleCreateRequest(BaseModel):
    kind: ReadAlertKind
    conditions: dict[str, Any] = {}
    channel: Channel
    target: str
    enabled: bool = True

    @model_validator(mode="after")
    def _check_target_matches_channel(self) -> AlertRuleCreateRequest:
        if self.channel is Channel.EMAIL:
            raise ValueError(
                "the email channel has no configured mail transport; use webhook"
            )
        if not _URL_PATTERN.match(self.target):
            raise ValueError(f"target {self.target!r} is not a well-formed webhook URL")
        return self


class AlertRuleResponse(BaseModel):
    id: uuid.UUID
    monitor_id: uuid.UUID
    kind: ReadAlertKind
    conditions: dict[str, Any]
    channel: Channel
    target: str
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}
