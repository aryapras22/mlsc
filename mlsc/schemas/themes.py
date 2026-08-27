"""Request and response contracts for the theme lifecycle: reading and
editing a theme monitor's queries, reading its discovered candidates, and
polling a generation or discovery job.

Three of the four schemas here are pure response shapes over rows or JSONB
that already exist and were validated when they were written — a theme
seed's queries by ``run_query_generation``/``ThemeService.review_queries``,
a candidate by ``run_discovery`` — so there is nothing left to check on the
way out. The one request body, ``ReviewQueriesRequest``, carries a user's
edited query set, and per design.md's "Trust boundary" it needs no business
validation beyond the structure pydantic already gives: every edited query
becomes ``accepted=True`` unconditionally, which is what review means, so
there is no acceptance flag or rationale content to reject here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from mlsc.db.models import CandidateState, SourceName, ThemeJobKind, ThemeJobStatus


class QuerySetResponse(BaseModel):
    """One query in a theme seed's ``queries`` list (design.md, "Domain
    shapes"). Returned as ``list[QuerySetResponse]``, matching how
    ``MonitorSourceResponse`` is returned as a list rather than wrapped in
    a container model."""

    text: str
    rationale: str
    accepted: bool


class ReviewedQueryItem(BaseModel):
    text: str
    rationale: str


class ReviewQueriesRequest(BaseModel):
    """Body for ``PUT /monitors/{id}/theme/queries``.

    A wrapper model, not a bare list body: every other request schema in
    this codebase (``OverrideRequest``, ``MonitorSourceCreateRequest``,
    ``AlertRuleCreateRequest``) is a named model, and a bare top-level list
    would be the only exception. ``ReviewedQueryItem`` is a schema-layer
    type distinct from ``ThemeService``'s ``ReviewedQuery`` rather than a
    re-export of it — schemas have no dependency on the application layer
    anywhere else in this codebase, and importing across that boundary here
    would be the first instance of it. The router converts one to the other
    at the call site.
    """

    queries: list[ReviewedQueryItem]


class CandidateResponse(BaseModel):
    id: uuid.UUID
    monitor_id: uuid.UUID
    source_name: SourceName
    entity_ref: str
    display_name: str
    reason: str
    proposed_by_query: str
    provenance: dict[str, Any]
    state: CandidateState
    created_at: datetime
    reviewed_at: datetime | None

    model_config = {"from_attributes": True}


class ThemeJobResponse(BaseModel):
    id: uuid.UUID
    monitor_id: uuid.UUID
    kind: ThemeJobKind
    status: ThemeJobStatus
    submitted_at: datetime
    finished_at: datetime | None
    error: str | None

    model_config = {"from_attributes": True}
