"""The daily rollup task: turns one bucket's enriched, assigned documents into
its metrics rows, for every bucket a caller names.

Takes a bucket set rather than a single date, because any bucket can need
recomputing at any time — late arrival is the norm here, not the exception
(design.md, "Dependencies, injected"; learn.md, "Late arrival is normal").
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.config import TrendDetectionSettings
from mlsc.db.models import (
    Assignment,
    DailyMetric,
    Document,
    Enrichment,
    EventKind,
    GateOutcome,
    IngestionRun,
    Monitor,
    RollupReason,
    RunStatus,
    Topic,
    TopicStatus,
)
from mlsc.pipeline.analytics.buckets import bucket_for, bucket_range_utc
from mlsc.pipeline.analytics.correction import apply as apply_correction
from mlsc.pipeline.analytics.detectors.base import Candidate
from mlsc.pipeline.analytics.detectors import (
    burst as burst_detector,
    changepoint as changepoint_detector,
    direction as direction_detector,
    emergence as emergence_detector,
    novelty as novelty_detector,
    poisson_exact as poisson_detector,
    robust_z as robust_z_detector,
)
from mlsc.pipeline.analytics.evidence import NoEvidenceAvailable, select as select_evidence
from mlsc.pipeline.analytics.gates import baseline_sufficient, cooldown as cooldown_gate, volume_floor
from mlsc.pipeline.analytics.group import rollup_bucket
from mlsc.pipeline.analytics.normalization import load_context
from mlsc.pipeline.analytics.rollup import row_from
from mlsc.pipeline.analytics.scoring import TopicScoreInput, score_topics
from mlsc.pipeline.analytics.seasonality import remove_weekly
from mlsc.pipeline.analytics.series import Series, build_series
from mlsc.repositories.metrics import MetricRepository
from mlsc.repositories.trends import TrendEventRepository, TrendScoreRepository

logger = logging.getLogger(__name__)

_SERIES_WINDOW_DAYS = 60
_TEST_WINDOW_DAYS = 21


@dataclasses.dataclass(frozen=True)
class BucketOutcome:
    bucket: date
    written: int
    pruned: int
    absent_sources: int


@dataclasses.dataclass(frozen=True)
class RollupOutcome:
    completed: list[BucketOutcome]
    failed: list[tuple[date, str]]


async def rollup_daily(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    buckets: list[date],
    reason: RollupReason,
) -> RollupOutcome:
    """Recompute every bucket in ``buckets`` independently, completing the
    others when one fails (design.md, "Failure strategy": "Partial bucket
    failure inside a multi-bucket request")."""
    completed: list[BucketOutcome] = []
    failed: list[tuple[date, str]] = []

    for bucket in buckets:
        try:
            outcome = await _rollup_one_bucket(
                session_factory, monitor_id=monitor_id, bucket=bucket, reason=reason
            )
        except Exception as error:  # noqa: BLE001 - one bad bucket must not abort the batch
            logger.exception("rollup failed for monitor %s bucket %s", monitor_id, bucket)
            failed.append((bucket, str(error)))
            continue
        completed.append(outcome)

    return RollupOutcome(completed=completed, failed=failed)


async def _rollup_one_bucket(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    bucket: date,
    reason: RollupReason,
) -> BucketOutcome:
    async with session_factory() as session:
        monitor = await session.get(Monitor, monitor_id)
        timezone = monitor.timezone

        sample = await load_context(session, monitor_id, bucket, timezone=timezone)
        for absent in sample.absent:
            logger.info(
                "source %s absent for monitor %s bucket %s: %s",
                absent.source_name.value, monitor_id, bucket, absent.reason,
            )

        contexts_by_source = {context.source_name: context for context in sample.contexts}
        rows_by_source_topic = await _load_grouped_rows(session, monitor_id, bucket, timezone=timezone)

        grouped = rollup_bucket(rows_by_source_topic, contexts_by_source)

        repo = MetricRepository(session)
        await repo.upsert_bucket(monitor_id=monitor_id, bucket=bucket, rows=grouped, reason=reason)

        surviving_keys = {
            (row.source_name.value if row.source_name else None, row.topic_id) for row in grouped
        }
        pruned = await repo.prune_unsupported(
            monitor_id=monitor_id, bucket=bucket, surviving_keys=surviving_keys
        )

        await session.commit()

    return BucketOutcome(
        bucket=bucket, written=len(grouped), pruned=pruned, absent_sources=len(sample.absent)
    )


async def run_daily_analytics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    run_date: date,
) -> RollupOutcome:
    """The daily sequence's tail: assign today's enriched documents to
    topics, then roll up ``run_date`` — registered after topic assignment
    because a document's topic (or lack of one) is exactly what the
    by-topic breakdown groups on (requirement 1, 6; design.md, "Success
    path").

    Only ``run_date`` is rolled up here, not every bucket a source's
    documents happen to be published on: the sample context a bucket needs
    (``SampleContext``) comes from the ``FetchStats`` ledger keyed to
    ``IngestionRun.run_date``, and a cursor-based source's page can legally
    return an item published days before the run that collected it. Such an
    item is not a late arrival in the requirement-6 sense — the run that
    collected it is exactly today's run — it simply is not the sample this
    layer has a denominator for. It is correctly folded into ``run_date``'s
    own figures by ``_load_grouped_rows``, which groups by publish bucket
    inside the range this call rolls up, not implicitly claimed by a bucket
    with no run of its own.

    Discovery, refit and dormancy are not called here: they run weekly and
    monthly respectively (design.md, "Success path": "Three graphs on three
    cadences"), on their own schedule rather than this daily one.
    """
    from mlsc.config import load_topic_thresholds
    from mlsc.tasks.topics import assign_topics

    await assign_topics(
        session_factory, monitor_id=monitor_id, thresholds=load_topic_thresholds(), today=run_date
    )
    return await rollup_daily(
        session_factory, monitor_id=monitor_id, buckets=[run_date], reason=RollupReason.SCHEDULED
    )


async def recompute_affected(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    document_ids: list[uuid.UUID],
) -> RollupOutcome:
    """Requirement 6: a late-arriving document recomputes only the buckets it
    actually falls in, not the whole series (design.md, "Success path":
    ``recompute_affected``).
    """
    if not document_ids:
        return RollupOutcome(completed=[], failed=[])

    async with session_factory() as session:
        monitor = await session.get(Monitor, monitor_id)
        timezone = monitor.timezone

        result = await session.execute(
            select(Document.published_at).where(Document.id.in_(document_ids))
        )
        published_ats = [row[0] for row in result.all()]

    buckets = sorted({bucket_for(instant, timezone=timezone) for instant in published_ats})
    return await rollup_daily(
        session_factory, monitor_id=monitor_id, buckets=buckets, reason=RollupReason.LATE_ARRIVAL
    )


async def _load_grouped_rows(
    session: AsyncSession, monitor_id: uuid.UUID, bucket: date, *, timezone: str
) -> dict[tuple, list]:
    bucket_start, bucket_end = bucket_range_utc(bucket, timezone=timezone)
    result = await session.execute(
        select(Document, Enrichment, Assignment.topic_id)
        .join(Enrichment, Enrichment.document_id == Document.id)
        .outerjoin(Assignment, Assignment.document_id == Document.id)
        .where(
            Document.monitor_id == monitor_id,
            Document.published_at >= bucket_start,
            Document.published_at < bucket_end,
            Enrichment.is_relevant.is_(True),
        )
    )

    grouped: dict[tuple, list] = {}
    for document, enrichment, topic_id in result.all():
        key = (document.source_name, topic_id)
        grouped.setdefault(key, []).append(row_from(document, enrichment))
    return grouped


@dataclasses.dataclass(frozen=True)
class DetectionOutcome:
    events_written: int
    gates_recorded: int
    candidates_before_correction: int
    candidates_after_correction: int


async def detect_trends(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    bucket: date,
    settings: TrendDetectionSettings,
) -> DetectionOutcome:
    """Requirement 1: for a monitor and a date, find the changes worth
    surfacing — gated, corrected across the whole day's tests together, and
    only then written as events (design.md, "Success path": ``detect_trends``).
    """
    async with session_factory() as session:
        monitor = await session.get(Monitor, monitor_id)
        timezone = monitor.timezone
        topics = await _active_topics(session, monitor_id)

        all_candidates: list[Candidate] = []
        gate_outcomes: list[GateOutcome] = []

        for topic in topics:
            series = await _build_topic_series(session, topic.id, bucket, monitor_id=monitor_id)

            floor_outcome = volume_floor(series, bucket, min_volume_floor=settings.min_volume_floor)
            if not floor_outcome.passed:
                gate_outcomes.append(_to_gate_row(monitor_id, floor_outcome))
                continue

            baseline_outcome, baseline = baseline_sufficient(
                series, bucket, min_clean_baseline_days=settings.min_clean_baseline_days
            )
            if not baseline_outcome.passed:
                gate_outcomes.append(_to_gate_row(monitor_id, baseline_outcome))
                # Emergence has no baseline to test against by definition,
                # so it runs even when the baseline gate fails a brand-new topic.
                emergence_candidate = await _test_emergence(session, topic, bucket)
                if emergence_candidate is not None:
                    all_candidates.append(emergence_candidate)
                continue

            gate_outcomes.append(GateOutcome(monitor_id=monitor_id, topic_id=topic.id, bucket=bucket, passed=True))

            deseasonalised = remove_weekly(series)
            candidates = await _run_detectors(
                session, topic, deseasonalised.series, baseline, bucket, settings, timezone=timezone
            )
            all_candidates.extend(candidates)

        candidates_before = len(all_candidates)
        survivors = apply_correction(all_candidates, alpha=settings.fdr_alpha)
        candidates_after = len(survivors)

        events_written = 0
        for candidate in survivors:
            last_dates = await TrendEventRepository(session).last_event_dates(
                candidate.topic_id, before=bucket, lookback_days=settings.cooldown_days * 4
            )
            cooldown_outcome = cooldown_gate(
                candidate.topic_id, bucket, candidate.kind,
                last_event_dates=last_dates, cooldown_days=settings.cooldown_days,
            )
            if not cooldown_outcome.passed:
                gate_outcomes.append(_to_gate_row(monitor_id, cooldown_outcome))
                continue

            try:
                evidence_ids = await select_evidence(
                    session, topic_id=candidate.topic_id, bucket=bucket, method=candidate.method
                )
            except NoEvidenceAvailable:
                # An event without evidence violates C4: this must crash
                # before the event is written, not degrade into one.
                raise

            await TrendEventRepository(session).upsert_event(
                monitor_id=monitor_id,
                topic_id=candidate.topic_id,
                detected_on=bucket,
                kind=candidate.kind,
                method=candidate.method,
                severity=abs(candidate.test_result.statistic),
                statistics=dataclasses.asdict(candidate.test_result) | {
                    "method": candidate.test_result.method.value,
                    "direction": candidate.test_result.direction.value,
                },
                evidence_ids=evidence_ids,
            )
            events_written += 1

        for outcome in gate_outcomes:
            session.add(outcome)

        await session.commit()

    return DetectionOutcome(
        events_written=events_written,
        gates_recorded=len(gate_outcomes),
        candidates_before_correction=candidates_before,
        candidates_after_correction=candidates_after,
    )


async def _active_topics(session: AsyncSession, monitor_id: uuid.UUID) -> list[Topic]:
    result = await session.execute(
        select(Topic).where(Topic.monitor_id == monitor_id, Topic.status == TopicStatus.ACTIVE)
    )
    return list(result.scalars().all())


async def _build_topic_series(
    session: AsyncSession, topic_id: uuid.UUID, bucket: date, *, monitor_id: uuid.UUID
) -> Series:
    window_start = bucket - timedelta(days=_SERIES_WINDOW_DAYS - 1)
    window_dates = [window_start + timedelta(days=offset) for offset in range(_SERIES_WINDOW_DAYS)]

    metrics_result = await session.execute(
        select(DailyMetric).where(
            DailyMetric.topic_id == topic_id,
            DailyMetric.source_name.is_(None),
            DailyMetric.bucket >= window_start,
            DailyMetric.bucket <= bucket,
        )
    )
    metrics_by_bucket = {row.bucket: row for row in metrics_result.scalars().all()}

    runs_result = await session.execute(
        select(IngestionRun.run_date, IngestionRun.status).where(
            IngestionRun.monitor_id == monitor_id,
            IngestionRun.run_date >= window_start,
            IngestionRun.run_date <= bucket,
        )
    )
    run_status_by_bucket = dict(runs_result.all())

    return build_series(topic_id, window_dates, metrics_by_bucket, run_status_by_bucket)


async def _test_emergence(session: AsyncSession, topic: Topic, bucket: date) -> Candidate | None:
    metric = await session.execute(
        select(DailyMetric.doc_count).where(
            DailyMetric.topic_id == topic.id, DailyMetric.source_name.is_(None),
            DailyMetric.bucket == bucket,
        )
    )
    doc_count = metric.scalar_one_or_none()
    if doc_count is None:
        return None
    result = emergence_detector.test(topic.first_seen, bucket, doc_count)
    if result is None:
        return None
    return Candidate(topic_id=topic.id, kind=EventKind.EMERGENCE, method=result.method, test_result=result)


async def _run_detectors(
    session: AsyncSession, topic: Topic, series: Series, baseline, bucket: date,
    settings: TrendDetectionSettings, *, timezone: str,
) -> list[Candidate]:
    """One row per surviving detector, each wrapped so a single method
    raising is recorded and skipped rather than aborting the ensemble
    (design.md, "Failure strategy": ``DetectorFailed`` falls back)."""
    candidates: list[Candidate] = []

    def add(kind: EventKind, method_module, **kwargs) -> None:
        try:
            result = method_module.test(series, baseline, **kwargs)
        except Exception:  # noqa: BLE001 - one detector failing must not abort the ensemble
            logger.exception("detector %s failed for topic %s", method_module.__name__, topic.id)
            return
        if result is not None:
            candidates.append(Candidate(topic_id=topic.id, kind=kind, method=result.method, test_result=result))

    add(EventKind.BURST, robust_z_detector, z_threshold=settings.burst_z_threshold)
    add(EventKind.BURST, poisson_detector, alpha=settings.fdr_alpha)
    add(EventKind.BURST, burst_detector, scale_factor=settings.burst_z_threshold / 2)

    direction_result = None
    try:
        direction_result = direction_detector.test(
            series, baseline, window_days=_TEST_WINDOW_DAYS, alpha=settings.fdr_alpha
        )
    except Exception:  # noqa: BLE001
        logger.exception("detector direction failed for topic %s", topic.id)
    if direction_result is not None:
        kind = EventKind.SUSTAINED_GROWTH if direction_result.direction.value == "rising" else EventKind.DECLINE
        candidates.append(
            Candidate(topic_id=topic.id, kind=kind, method=direction_result.method, test_result=direction_result)
        )

    add(
        EventKind.CHANGEPOINT, changepoint_detector,
        window_days=_TEST_WINDOW_DAYS, alpha=settings.fdr_alpha,
    )

    sentiment_result = None
    try:
        sentiment_result = changepoint_detector.test(
            series, baseline, window_days=_TEST_WINDOW_DAYS, alpha=settings.fdr_alpha,
            value_of=lambda point: point.sentiment_mean,
        )
    except Exception:  # noqa: BLE001
        logger.exception("detector sentiment-changepoint failed for topic %s", topic.id)
    if sentiment_result is not None:
        candidates.append(
            Candidate(
                topic_id=topic.id, kind=EventKind.SENTIMENT_FLIP,
                method=sentiment_result.method, test_result=sentiment_result,
            )
        )

    novelty_candidate = await _test_novelty(session, topic, bucket, timezone=timezone)
    if novelty_candidate is not None:
        candidates.append(novelty_candidate)

    return candidates


async def _test_novelty(
    session: AsyncSession, topic: Topic, bucket: date, *, timezone: str
) -> Candidate | None:
    bucket_start, bucket_end = bucket_range_utc(bucket, timezone=timezone)
    history_start = bucket - timedelta(days=_TEST_WINDOW_DAYS)

    today_result = await session.execute(
        select(Document.body)
        .join(Assignment, Assignment.document_id == Document.id)
        .where(
            Assignment.topic_id == topic.id,
            Document.published_at >= bucket_start, Document.published_at < bucket_end,
        )
    )
    today_texts = [body for (body,) in today_result.all() if body]

    history_result = await session.execute(
        select(Document.body)
        .join(Assignment, Assignment.document_id == Document.id)
        .where(
            Assignment.topic_id == topic.id,
            Document.published_at >= history_start, Document.published_at < bucket_start,
        )
    )
    history_texts = [body for (body,) in history_result.all() if body]

    result = novelty_detector.test(today_texts, history_texts)
    if result is None:
        return None
    return Candidate(topic_id=topic.id, kind=EventKind.NOVELTY, method=result.method, test_result=result)


def _to_gate_row(monitor_id: uuid.UUID, outcome) -> GateOutcome:
    return GateOutcome(
        monitor_id=monitor_id, topic_id=outcome.topic_id, bucket=outcome.bucket,
        passed=outcome.passed, reason=outcome.reason,
    )


async def score_trends(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    bucket: date,
    settings: TrendDetectionSettings,
) -> int:
    """Requirement 9: one composite score per topic with metrics that day,
    run as the pipeline's final step over that day's already-written events
    (design.md, "Success path": ``score_topics``) — never before detection,
    since a topic's burst or novelty component depends on the events
    detection just wrote for it.
    """
    async with session_factory() as session:
        day_is_trustworthy = await _day_is_trustworthy(session, monitor_id, bucket)

        metrics_result = await session.execute(
            select(DailyMetric).where(
                DailyMetric.monitor_id == monitor_id, DailyMetric.bucket == bucket,
                DailyMetric.topic_id.is_not(None), DailyMetric.source_name.is_(None),
            )
        )
        metrics_by_topic = {row.topic_id: row for row in metrics_result.scalars().all()}
        if not metrics_by_topic:
            return 0

        events = await TrendEventRepository(session).events_for_bucket(monitor_id, bucket)
        events_by_topic: dict[uuid.UUID, list] = {}
        for event in events:
            events_by_topic.setdefault(event.topic_id, []).append(event)

        active_source_count = await _active_source_count(session, monitor_id, bucket)

        inputs = []
        for topic_id, metric in metrics_by_topic.items():
            sources_with_topic = await _sources_with_topic(session, monitor_id, topic_id, bucket)
            inputs.append(
                TopicScoreInput(
                    topic_id=topic_id,
                    events=events_by_topic.get(topic_id, []),
                    metric=metric,
                    author_diversity=metric.author_diversity,
                    active_source_count=active_source_count,
                    sources_with_topic=sources_with_topic,
                )
            )

        scores = score_topics(inputs, settings=settings, day_is_trustworthy=day_is_trustworthy)

        score_repo = TrendScoreRepository(session)
        for topic_score in scores:
            await score_repo.upsert_score(
                monitor_id=monitor_id, topic_id=topic_score.topic_id, bucket=bucket,
                value=topic_score.value, components=topic_score.components or {},
                penalties=topic_score.penalties, withheld_reason=topic_score.withheld_reason,
            )

        await session.commit()

    return len(scores)


async def _day_is_trustworthy(session: AsyncSession, monitor_id: uuid.UUID, bucket: date) -> bool:
    """C5/C6: a day is untrustworthy when its own run was not clean —
    reusing the same partial/quota signals the series and gates already
    treat as disqualifying (design.md, "Domain shapes": ``PointQuality``)."""
    result = await session.execute(
        select(IngestionRun.status).where(
            IngestionRun.monitor_id == monitor_id, IngestionRun.run_date == bucket
        )
    )
    statuses = [row[0] for row in result.all()]
    if not statuses:
        return True  # no run row for this date is not itself a trust signal
    return all(status != RunStatus.PARTIAL for status in statuses)


async def _active_source_count(session: AsyncSession, monitor_id: uuid.UUID, bucket: date) -> int:
    result = await session.execute(
        select(DailyMetric.source_name).where(
            DailyMetric.monitor_id == monitor_id, DailyMetric.bucket == bucket,
            DailyMetric.source_name.is_not(None), DailyMetric.topic_id.is_(None),
        )
    )
    return len({row[0] for row in result.all()})


async def _sources_with_topic(
    session: AsyncSession, monitor_id: uuid.UUID, topic_id: uuid.UUID, bucket: date
) -> int:
    result = await session.execute(
        select(DailyMetric.source_name).where(
            DailyMetric.monitor_id == monitor_id, DailyMetric.bucket == bucket,
            DailyMetric.topic_id == topic_id, DailyMetric.source_name.is_not(None),
        )
    )
    return len({row[0] for row in result.all()})
