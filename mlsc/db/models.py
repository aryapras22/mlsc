from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, MetaData, UniqueConstraint
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


class TopicStatus(str, Enum):
    """A topic's lifecycle. No ``deleted`` member — that is the point of C3."""

    ACTIVE = "active"
    DORMANT = "dormant"
    MERGED = "merged"
    ARCHIVED = "archived"


class Topic(Base):
    """A persistent theme. ``id`` is the one identifier that must outlive every
    algorithm that produced it — merging and dormancy change ``status``, never
    the primary key (design.md, "Domain shapes").
    """

    __tablename__ = "topics"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str]
    keywords: Mapped[list[str]] = mapped_column(JSONB, default=list)
    centroid: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    doc_count: Mapped[int] = mapped_column(default=0)
    first_seen: Mapped[date]
    last_seen: Mapped[date]
    status: Mapped[TopicStatus] = mapped_column(default=TopicStatus.ACTIVE)
    merged_into: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL")
    )
    drift_score: Mapped[float] = mapped_column(default=0.0)
    is_pinned: Mapped[bool] = mapped_column(default=False)
    label_is_provisional: Mapped[bool] = mapped_column(default=False)
    label_provider: Mapped[str | None]
    label_model: Mapped[str | None]
    label_prompt_version: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AssignmentMethod(str, Enum):
    """How a document arrived at its topic. What makes "never reassign a
    manual assignment" a checkable condition rather than a convention."""

    CENTROID = "centroid"
    CLUSTERED = "clustered"
    MANUAL = "manual"


class Assignment(Base):
    """One document, one topic. A second row for the same document replaces the
    first rather than coexisting — prevalence must sum to one (design.md,
    "Domain shapes").
    """

    __tablename__ = "assignments"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_assignments_document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), index=True
    )
    similarity: Mapped[float]
    method: Mapped[AssignmentMethod]
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LineageEvent(str, Enum):
    MERGE = "merge"
    REFIT_REMAP = "refit_remap"
    SPLIT_PROPOSED = "split_proposed"


class Lineage(Base):
    """A record of what happened to a topic identifier. Both ``from_topic`` and
    ``to_topic`` stay resolvable after a merge (requirement 5)."""

    __tablename__ = "lineage"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    from_topic: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), index=True
    )
    to_topic: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL")
    )
    event: Mapped[LineageEvent]
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reason: Mapped[str | None]


class SplitProposalStatus(str, Enum):
    OPEN = "open"
    DISMISSED = "dismissed"


class SplitProposal(Base):
    """A recorded suggestion that a topic covers two things. Stored because it
    is a user-facing artefact (requirement 8) — never auto-applied."""

    __tablename__ = "split_proposals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), index=True
    )
    evidence: Mapped[str]
    drift_score: Mapped[float]
    status: Mapped[SplitProposalStatus] = mapped_column(default=SplitProposalStatus.OPEN)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RollupReason(str, Enum):
    """Why a bucket was (re)computed. A scheduled run and a late-arrival
    recompute produce the same numbers by a different path, and knowing
    which is what makes requirement 6 debuggable (design.md, "Domain
    shapes")."""

    SCHEDULED = "scheduled"
    LATE_ARRIVAL = "late_arrival"
    TOPIC_MERGE = "topic_merge"
    MANUAL = "manual"


class DailyMetric(Base):
    """One breakdown's figures for one monitor and one bucket.

    ``source_name`` and ``topic_id`` are each nullable, meaning "every
    source" or "every topic" — the across-source and across-topic aggregates
    are ordinary rows with an all-value rather than a separate table
    (design.md, "Domain shapes": ``MetricKey``). Uniqueness over the nullable
    pair is enforced by four migration-owned partial indexes rather than a
    plain unique constraint, because SQL treats two NULLs as distinct and
    would otherwise let duplicate all-value rows accumulate silently.
    """

    __tablename__ = "daily_metrics"
    __table_args__ = (
        Index("ix_daily_metrics_monitor_bucket", "monitor_id", "bucket"),
        Index(
            "uq_daily_metrics_key_both", "monitor_id", "bucket", "source_name", "topic_id",
            unique=True,
            postgresql_where="source_name IS NOT NULL AND topic_id IS NOT NULL",
        ),
        Index(
            "uq_daily_metrics_key_source_only", "monitor_id", "bucket", "source_name",
            unique=True,
            postgresql_where="source_name IS NOT NULL AND topic_id IS NULL",
        ),
        Index(
            "uq_daily_metrics_key_topic_only", "monitor_id", "bucket", "topic_id",
            unique=True,
            postgresql_where="source_name IS NULL AND topic_id IS NOT NULL",
        ),
        Index(
            "uq_daily_metrics_key_neither", "monitor_id", "bucket",
            unique=True,
            postgresql_where="source_name IS NULL AND topic_id IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    bucket: Mapped[date] = mapped_column(index=True)
    source_name: Mapped[SourceName | None]
    topic_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("topics.id", ondelete="SET NULL"), index=True
    )
    doc_count: Mapped[int]
    doc_count_share: Mapped[float]
    sample_size: Mapped[int]
    quota_hit: Mapped[bool] = mapped_column(default=False)
    sentiment_mean: Mapped[float | None]
    sentiment_p25: Mapped[float | None]
    negativity_rate: Mapped[float | None]
    engagement_sum: Mapped[int | None]
    author_diversity: Mapped[float | None]
    rating_mean: Mapped[float | None]
    intent_counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    reason: Mapped[RollupReason]
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EventKind(str, Enum):
    """What a user sees. Kept separate from ``Method`` (design.md, "Domain
    shapes") — a burst found by two methods is still one kind a user can
    filter their feed on."""

    BURST = "burst"
    SUSTAINED_GROWTH = "sustained_growth"
    DECLINE = "decline"
    SENTIMENT_FLIP = "sentiment_flip"
    CHANGEPOINT = "changepoint"
    EMERGENCE = "emergence"
    NOVELTY = "novelty"


class DetectionMethod(str, Enum):
    """How a candidate was found."""

    ROBUST_Z = "robust_z"
    POISSON_EXACT = "poisson_exact"
    KLEINBERG = "kleinberg"
    MANN_KENDALL = "mann_kendall"
    PELT = "pelt"
    CTFIDF_DELTA = "ctfidf_delta"


class Direction(str, Enum):
    RISING = "rising"
    FALLING = "falling"


class GateReason(str, Enum):
    """Why a topic produced no event on a day detection ran. Recorded even
    on the failing path (design.md, "Domain shapes": ``GateOutcome``), so
    "nothing fired" is distinguishable from "detection never ran"."""

    BELOW_VOLUME_FLOOR = "below_volume_floor"
    INSUFFICIENT_BASELINE = "insufficient_baseline"
    COOLDOWN_ACTIVE = "cooldown_active"
    CORRECTED_AWAY = "corrected_away"
    DAY_UNTRUSTWORTHY = "day_untrustworthy"


class TrendEvent(Base):
    """One detected change for one topic on one day.

    Unique over monitor, topic, date and kind (C12): a re-run of the same
    date converges on the same row rather than duplicating, which is what
    makes re-running a date idempotent without a lock (design.md, "Failure
    strategy": "Event upsert conflict").
    """

    __tablename__ = "trend_events"
    __table_args__ = (
        UniqueConstraint(
            "monitor_id", "topic_id", "detected_on", "kind",
            name="uq_trend_events_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), index=True
    )
    detected_on: Mapped[date] = mapped_column(index=True)
    kind: Mapped[EventKind]
    method: Mapped[DetectionMethod]
    severity: Mapped[float]
    statistics: Mapped[dict] = mapped_column(JSONB, default=dict)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GateOutcome(Base):
    """One topic's pass/fail through detection's gates for one day.

    Written for the passing case too, not only the failing one: requirement
    1 asks what happened for a monitor and a date, and a topic that cleared
    every gate but produced no significant test is as much an answer as one
    that was floored out.
    """

    __tablename__ = "gate_outcomes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), index=True
    )
    bucket: Mapped[date] = mapped_column(index=True)
    passed: Mapped[bool]
    reason: Mapped[GateReason | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TrendScore(Base):
    """One topic's composite ranking score for one day.

    ``withheld_reason`` set means ``value`` and ``components`` carry no
    meaning for that row — withheld rather than computed from a floor, since
    a score computed from bad data would look as confident as a real one
    (design.md, "Dependencies, injected"; requirements.md, C5/C6).
    """

    __tablename__ = "trend_scores"
    __table_args__ = (
        UniqueConstraint("monitor_id", "topic_id", "bucket", name="uq_trend_scores_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    monitor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), index=True
    )
    bucket: Mapped[date] = mapped_column(index=True)
    value: Mapped[float | None]
    components: Mapped[dict] = mapped_column(JSONB, default=dict)
    penalties: Mapped[dict] = mapped_column(JSONB, default=dict)
    withheld_reason: Mapped[str | None]
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
