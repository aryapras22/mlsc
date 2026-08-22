"""Request and response contracts for operator-initiated repairs.

The stage and the window are checked once here, at the trust boundary
(design.md, "Trust boundary"): a stage outside the closed set or a window
that is unordered or reaches into the future is rejected before
``OverrideService`` ever sees it. The purge token is *not* validated here —
whether it matches the count it was issued for depends on the current
database state, which is `OverrideService.submit`'s job, not a stateless
schema's (design.md, "Domain shapes": `PurgeNotConfirmed`).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, model_validator

from mlsc.db.models import OverrideKind, OverrideStatus
from mlsc.pipeline.stages import Stage


class OverrideRequest(BaseModel):
    kind: OverrideKind
    stage: Stage | None = None
    window_start: date | None = None
    window_end: date | None = None
    purge_token: str | None = None

    @model_validator(mode="after")
    def _check_kind_parameters(self) -> OverrideRequest:
        if self.kind is OverrideKind.STAGE_RERUN:
            if self.stage is None:
                raise ValueError("stage is required for a stage_rerun override")
        elif self.kind is OverrideKind.BACKFILL_WINDOW:
            if self.window_start is None or self.window_end is None:
                raise ValueError("window_start and window_end are required for a backfill_window override")
            if self.window_start > self.window_end:
                raise ValueError("window_start must not be after window_end")
            if self.window_end > date.today():
                raise ValueError("window_end must not be in the future")
        return self


class RetentionPreviewResponse(BaseModel):
    count: int
    token: str


class OverrideSubmitResponse(BaseModel):
    job_id: uuid.UUID


class OverrideJobView(BaseModel):
    id: uuid.UUID
    monitor_id: uuid.UUID
    kind: OverrideKind
    parameters: dict[str, Any]
    status: OverrideStatus
    submitted_at: datetime
    finished_at: datetime | None
    outcome: dict[str, Any] | None

    model_config = {"from_attributes": True}
