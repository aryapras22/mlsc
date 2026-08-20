from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, MetaData, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

EMBEDDING_DIMENSIONS = 384

_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative root whose metadata is the Alembic autogenerate target.

    Alembic owns every schema object; nothing may call ``Base.metadata.create_all``.
    """

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


class TargetType(str, Enum):
    """What a monitor watches. Closed: seed validation depends on knowing which."""

    PRODUCT = "product"
    THEME = "theme"


class MonitorStatus(str, Enum):
    """A monitor's lifecycle. There is no terminal success state (handoff §1.2)."""

    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class Monitor(Base):
    """What the system watches. Every document, topic, and metric is owned by one."""

    __tablename__ = "monitors"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str]
    target_type: Mapped[TargetType]
    seed: Mapped[dict] = mapped_column(JSONB)
    schedule: Mapped[str]
    timezone: Mapped[str]
    status: Mapped[MonitorStatus] = mapped_column(default=MonitorStatus.ACTIVE)
    retention_days: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    registration: Mapped[ScheduleRegistration | None] = relationship(
        back_populates="monitor", cascade="all, delete-orphan"
    )


class ScheduleRegistration(Base):
    """The cron expression and timezone Beat projects for one monitor.

    At most one row per monitor: the unique constraint on ``monitor_id`` is what
    actually guarantees "at most one active registration" against a race, not
    application code (learn.md, "One transaction across a state machine").
    """

    __tablename__ = "schedule_registrations"
    __table_args__ = (UniqueConstraint("monitor_id", name="uq_schedule_registrations_monitor_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE")
    )
    cron_expression: Mapped[str]
    timezone: Mapped[str]

    monitor: Mapped[Monitor] = relationship(back_populates="registration")


class SourceName(str, Enum):
    """Which adapter a source row is collected by. Closed: only what is built."""

    PLAY = "play"
    APPSTORE = "appstore"
    DISCOURSE = "discourse"
    NEWS = "news"
    RSS = "rss"
    HACKERNEWS = "hackernews"


class RunStatus(str, Enum):
    """An ingestion run's lifecycle for one monitor on one date."""

    PENDING = "pending"
    RUNNING = "running"
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"


class FetchOutcomeKind(str, Enum):
    """What actually happened for one source in one run. Distinguishing
    ``skipped_disabled`` from a genuine zero is what requirement 4 needs."""

    COLLECTED = "collected"
    FAILED = "failed"
    SKIPPED_DISABLED = "skipped_disabled"


class QuotaOutcome(str, Enum):
    """Whether a run's daily allowance was reached.

    A named variant rather than a boolean, so meaning travels with the value:
    ``ALLOWANCE_REACHED`` means the paired count is a floor, not a measurement
    (learn.md, "A fixed daily quota as a stable denominator").
    """

    WITHIN_ALLOWANCE = "within_allowance"
    ALLOWANCE_REACHED = "allowance_reached"


class MonitorSource(Base):
    """One concrete source attached to a monitor: what to collect, and from where.

    ``instance_key`` is the source's own identifier for what it watches — a Play
    package id, a Discourse base URL — extracted from ``config`` at attach time
    so uniqueness does not depend on comparing JSONB contents.
    """

    __tablename__ = "monitor_sources"
    __table_args__ = (
        UniqueConstraint(
            "monitor_id", "source_name", "instance_key", name="uq_monitor_sources_identity"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    source_name: Mapped[SourceName]
    instance_key: Mapped[str]
    config: Mapped[dict] = mapped_column(JSONB)
    daily_quota: Mapped[int]
    enabled: Mapped[bool] = mapped_column(default=True)
    last_external_id: Mapped[str | None]
    last_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IngestionRun(Base):
    """One run of collection for one monitor on one date.

    Unique per monitor, date, and backfill mode so a scheduled run and a
    deliberate backfill for the same day can coexist without colliding.
    """

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        UniqueConstraint(
            "monitor_id", "run_date", "is_backfill", name="uq_ingestion_runs_identity"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    run_date: Mapped[date]
    is_backfill: Mapped[bool] = mapped_column(default=False)
    status: Mapped[RunStatus] = mapped_column(default=RunStatus.PENDING)
    stage_status: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FetchStats(Base):
    """One source's outcome for one run. Written even when nothing was collected.

    A record in its own right rather than columns on the run: a run has one row
    per source, and the row must exist whether the source succeeded, was
    truncated, or failed outright (design.md, "Domain shapes").
    """

    __tablename__ = "fetch_stats"
    __table_args__ = (
        UniqueConstraint("run_id", "monitor_source_id", name="uq_fetch_stats_run_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="CASCADE"))
    monitor_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitor_sources.id", ondelete="CASCADE")
    )
    attempted: Mapped[int] = mapped_column(default=0)
    fetched: Mapped[int] = mapped_column(default=0)
    duplicates: Mapped[int] = mapped_column(default=0)
    kept: Mapped[int] = mapped_column(default=0)
    quota: Mapped[int]
    quota_outcome: Mapped[QuotaOutcome] = mapped_column(default=QuotaOutcome.WITHIN_ALLOWANCE)
    validation_failed: Mapped[bool] = mapped_column(default=False)
    outcome_kind: Mapped[FetchOutcomeKind] = mapped_column(default=FetchOutcomeKind.COLLECTED)
    library_version: Mapped[str]
    duration_seconds: Mapped[float] = mapped_column(default=0.0)
    error: Mapped[str | None]


class SourceState(str, Enum):
    """A source's health. Distinct from ``MonitorSource.enabled``, which is the
    user's own switch; this is the system's computed verdict."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BROKEN = "broken"
    DISABLED = "disabled"


class AlertKind(str, Enum):
    EMPTY_STREAK = "empty_streak"
    VOLUME_COLLAPSE = "volume_collapse"
    SMOKE_FAILED = "smoke_failed"
    LIBRARY_STALE = "library_stale"


class SourceHealth(Base):
    """One row per monitor source: the latest computed verdict, not a history."""

    __tablename__ = "source_health"
    __table_args__ = (
        UniqueConstraint("monitor_source_id", name="uq_source_health_monitor_source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitor_sources.id", ondelete="CASCADE")
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_empty: Mapped[int] = mapped_column(default=0)
    consecutive_fail: Mapped[int] = mapped_column(default=0)
    rows_median_28d: Mapped[float | None]
    library_version: Mapped[str | None]
    state: Mapped[SourceState] = mapped_column(default=SourceState.HEALTHY)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ScraperAlert(Base):
    """Routed separately from product alerts (requirement 2) — its own table,
    not a kind column shared with anything else."""

    __tablename__ = "scraper_alerts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    monitor_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitor_sources.id", ondelete="CASCADE")
    )
    kind: Mapped[AlertKind]
    raised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    observed: Mapped[str | None]
    expected: Mapped[str | None]


class BackfillStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class BackfillJob(Base):
    """A one-shot wider-window collection. Never an IngestionRun row itself —
    it fans out into one IngestionRun(is_backfill=True) per date, so the daily
    series and its statistics are untouched (requirement 6, 7)."""

    __tablename__ = "backfill_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"))
    window_start: Mapped[date]
    window_end: Mapped[date]
    status: Mapped[BackfillStatus] = mapped_column(default=BackfillStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Document(Base):
    """One collected item. Author identity is a hash; nothing else identifies who wrote it.

    Unique on ``(monitor_id, source_name, external_id)``: the natural key that
    makes re-running collection for the same day idempotent at the database
    level rather than by an application-side existence check (learn.md,
    "Idempotency by natural key").
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint(
            "monitor_id", "source_name", "external_id", name="uq_documents_natural_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    source_name: Mapped[SourceName]
    external_id: Mapped[str]
    entity_id: Mapped[str]
    url: Mapped[str | None]
    author_hash: Mapped[str]
    body: Mapped[str | None]
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    rating: Mapped[int | None]
    app_version: Mapped[str | None]
    engagement: Mapped[int | None]
    content_hash: Mapped[str]
    raw: Mapped[dict] = mapped_column(JSONB)


class SentimentLabel(str, Enum):
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    POSITIVE = "positive"


class Intent(str, Enum):
    FEATURE_REQUEST = "feature_request"
    BUG_REPORT = "bug_report"
    PRAISE = "praise"
    COMPLAINT = "complaint"
    QUESTION = "question"
    CHURN_SIGNAL = "churn_signal"
    PRICING = "pricing"
    COMPETITOR_MENTION = "competitor_mention"
    SPAM = "spam"


class Enrichment(Base):
    """Everything derived from one document's text. One row per document.

    ``model_versions`` is one JSON map of stage name to model version rather
    than a column per stage, so "which documents have a stale embedding" is a
    query, not a migration (design.md, "Domain shapes").
    """

    __tablename__ = "enrichments"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_enrichments_document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    language: Mapped[str | None]
    language_confidence: Mapped[float | None]
    is_relevant: Mapped[bool] = mapped_column(default=True)
    relevance_score: Mapped[float | None]
    near_duplicate_of: Mapped[uuid.UUID | None]
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    sentiment_score: Mapped[float | None]
    sentiment_label: Mapped[SentimentLabel | None]
    intent: Mapped[Intent | None]
    intent_confidence: Mapped[float | None]
    llm_provider: Mapped[str | None]
    llm_model: Mapped[str | None]
    prompt_version: Mapped[str | None]
    model_versions: Mapped[dict] = mapped_column(JSONB, default=dict)
    enriched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
