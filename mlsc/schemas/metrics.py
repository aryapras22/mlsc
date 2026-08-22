"""Response contracts for the metrics read API — Pydantic schemas, never
ORM objects, so the wire format never leaks a column nobody meant to
publish (design.md, "Terminology": "Schema versus model").

``DataQuality`` is attached by the response wrapper, not built per endpoint
(design.md, "Domain shapes") — every schema here that represents a metric
surface carries one.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, model_validator

from mlsc.db.models import EventKind, InsightKind, SourceName


class Metric(str, Enum):
    VOLUME = "volume"
    PREVALENCE = "prevalence"
    SENTIMENT = "sentiment"
    ENGAGEMENT = "engagement"
    AUTHOR_DIVERSITY = "author_diversity"
    RATING = "rating"


class PointQuality(str, Enum):
    CLEAN = "clean"
    TRUNCATED = "truncated"
    PARTIAL = "partial"


class Absence(str, Enum):
    """Requirement 6 made into a type: four situations that must not all
    collapse into an empty list (learn.md, "Empty, absent, and unknown")."""

    NO_DATA = "no_data"
    UNKNOWN_TOPIC = "unknown_topic"
    UNKNOWN_SOURCE = "unknown_source"
    OUT_OF_RETENTION = "out_of_retention"


class DateRange(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _check_ordered_and_bounded(self) -> DateRange:
        if self.end < self.start:
            raise ValueError("end must not be before start")
        if self.end > date.today():
            raise ValueError("end must not extend into the future")
        return self


class DataQuality(BaseModel):
    sample_size: int
    truncated_days: list[date]
    sources_ok: list[SourceName]
    sources_failed: list[SourceName]
    topics_absent: list[uuid.UUID]


class SeriesPoint(BaseModel):
    bucket: date
    value: float | None
    quality: PointQuality


class Series(BaseModel):
    metric: Metric
    topic_id: uuid.UUID | None
    source: SourceName | None
    points: list[SeriesPoint]
    absence: Absence | None
    data_quality: DataQuality


class Overview(BaseModel):
    period: DateRange
    headline_figures: dict[str, float | None]
    data_quality: DataQuality


class TopicRankingEntry(BaseModel):
    topic_id: uuid.UUID
    label: str
    doc_count: int
    doc_count_share: float
    sentiment_mean: float | None
    trend_score: float | None
    breadth_ratio: float | None


class TopicRanking(BaseModel):
    entries: list[TopicRankingEntry]
    data_quality: DataQuality


class EventView(BaseModel):
    id: uuid.UUID
    topic_id: uuid.UUID
    detected_on: date
    kind: EventKind
    method: str
    severity: float
    statistics: dict[str, Any]
    evidence_ids: list[str]


class InsightView(BaseModel):
    id: uuid.UUID
    monitor_id: uuid.UUID
    topic_id: uuid.UUID | None
    period: DateRange
    kind: InsightKind
    title: str
    body: str
    who: str | None
    what: str | None
    why: str | None
    score: float | None
    score_components: dict[str, float]
    evidence_ids: list[str]
    llm_provider: str
    llm_model: str
    prompt_version: str


class DocumentView(BaseModel):
    id: uuid.UUID
    source_name: SourceName
    url: str | None
    body: str | None
    published_at: datetime
    rating: int | None
    topic_id: uuid.UUID | None


class DocumentPage(BaseModel):
    items: list[DocumentView]
    cursor: str | None
    total_known: int | None


class RunView(BaseModel):
    id: uuid.UUID
    monitor_id: uuid.UUID
    run_date: date
    status: str
    stage_status: dict[str, str]
    started_at: datetime | None
    finished_at: datetime | None


class RunSummaryView(BaseModel):
    id: uuid.UUID
    run_date: date
    status: str
    is_backfill: bool


class RunSourceStatsView(BaseModel):
    """One source's outcome within a run, for the progress panel's poll
    (on-demand-collection design.md, "Domain shapes": `RunProgress`)."""

    monitor_source_id: uuid.UUID
    attempted: int
    fetched: int
    kept: int
    quota: int
    quota_outcome: str
    validation_failed: bool
    error: str | None

    model_config = {"from_attributes": True}


class EntityComparisonRow(BaseModel):
    entity_id: str
    doc_count: int
    share_of_voice: float
    sentiment_mean: float | None


class EntityComparison(BaseModel):
    period: DateRange
    entries: list[EntityComparisonRow]
    data_quality: DataQuality
