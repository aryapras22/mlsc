"""Core tests for trend detection over synthetic series with injected
events. No stochastic algorithm in the loop.

Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any

import pytest
from sqlalchemy import pool, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.config import TrendDetectionSettings
from mlsc.db.models import (
    Assignment,
    AssignmentMethod,
    Base,
    DailyMetric,
    Document,
    Enrichment,
    EventKind,
    GateOutcome,
    GateReason,
    RollupReason,
    RunStatus,
    SentimentLabel,
    SourceName,
    TargetType,
    Topic,
    TrendEvent,
)
from mlsc.pipeline.analytics.correction import apply as apply_correction
from mlsc.pipeline.analytics.detectors.base import Candidate, TestResult
from mlsc.pipeline.analytics.evidence import NoEvidenceAvailable
from mlsc.pipeline.analytics.evidence import select as select_evidence
from mlsc.pipeline.analytics.gates import baseline_sufficient, cooldown, volume_floor
from mlsc.pipeline.analytics.seasonality import remove_weekly
from mlsc.pipeline.analytics.series import PointQuality, Series, SeriesPoint
from mlsc.pipeline.normalize import hash_author, hash_content
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.tasks.analytics import detect_trends
from mlsc.db.models import DetectionMethod, Direction

LOCAL_DATABASE_URL = "postgresql+asyncpg://mlsc:mlsc@localhost:55433/mlsc"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _reachable(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except (OperationalError, OSError):
        return False


async def _reset_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


@pytest.fixture
def session_factory() -> async_sessionmaker:
    engine = create_async_engine(LOCAL_DATABASE_URL, poolclass=pool.NullPool)
    if not run(_reachable(engine)):
        run(engine.dispose())
        pytest.skip("local Compose PostgreSQL is not reachable at localhost:55433")
    run(_reset_schema(engine))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    run(engine.dispose())


async def _make_monitor(session_factory: async_sessionmaker) -> uuid.UUID:
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Roblox", target_type=TargetType.PRODUCT, seed={"identifiers": ["x"]},
            cron_expression="0 3 * * *", timezone="UTC", retention_days=90,
        )
    )
    return monitor.id


async def _seed_topic_with_series(
    session_factory: async_sessionmaker,
    monitor_id: uuid.UUID,
    *,
    base: date,
    daily_counts: list[int],
    with_documents_on_last_day: bool = True,
) -> tuple[uuid.UUID, date]:
    """A topic with one ``DailyMetric`` row per day in ``daily_counts``, and
    (optionally) real assigned documents on the last day so evidence
    selection has something to find."""
    topic_id = uuid.uuid4()
    last_day = base + timedelta(days=len(daily_counts) - 1)

    async with session_factory() as session:
        session.add(
            Topic(
                id=topic_id, monitor_id=monitor_id, label="t", keywords=[], centroid=[0.1] * 384,
                doc_count=0, first_seen=base, last_seen=last_day,
            )
        )
        await session.flush()
        for offset, count in enumerate(daily_counts):
            bucket = base + timedelta(days=offset)
            session.add(
                DailyMetric(
                    id=uuid.uuid4(), monitor_id=monitor_id, bucket=bucket, source_name=None,
                    topic_id=topic_id, doc_count=count, doc_count_share=0.5, sample_size=100,
                    quota_hit=False, sentiment_mean=0.1, sentiment_p25=0.0, negativity_rate=0.2,
                    engagement_sum=1, author_diversity=0.8, rating_mean=4.0, intent_counts={},
                    reason=RollupReason.SCHEDULED,
                )
            )
        await session.commit()

        if with_documents_on_last_day:
            doc_ids = []
            for i in range(3):
                doc_id = uuid.uuid4()
                session.add(
                    Document(
                        id=doc_id, monitor_id=monitor_id, source_name=SourceName.PLAY,
                        external_id=f"r{i}-{topic_id}", entity_id="x", url=None,
                        author_hash=hash_author(f"u{i}"), body="a review",
                        published_at=datetime(last_day.year, last_day.month, last_day.day, 12, tzinfo=dt_timezone.utc),
                        rating=5, app_version="1", engagement=1,
                        content_hash=hash_content(f"r{i}-{topic_id}"), raw={},
                    )
                )
                doc_ids.append(doc_id)
            await session.flush()
            for doc_id in doc_ids:
                session.add(
                    Enrichment(
                        id=uuid.uuid4(), document_id=doc_id, is_relevant=True, embedding=[0.1] * 384,
                        sentiment_score=0.1, sentiment_label=SentimentLabel.POSITIVE, model_versions={},
                    )
                )
            await session.flush()
            for doc_id in doc_ids:
                session.add(
                    Assignment(
                        id=uuid.uuid4(), document_id=doc_id, topic_id=topic_id, similarity=0.9,
                        method=AssignmentMethod.CENTROID,
                    )
                )
            await session.commit()

    return topic_id, last_day


class TestSpikeDetection:
    def test_a_spike_is_detected_as_a_burst(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        base = date(2026, 6, 1)
        counts = [10] * 25 + [50]
        topic_id, bucket = run(
            _seed_topic_with_series(session_factory, monitor_id, base=base, daily_counts=counts)
        )

        settings = TrendDetectionSettings(
            min_volume_floor=5, min_clean_baseline_days=14, cooldown_days=3, fdr_alpha=0.1,
            burst_z_threshold=3.5,
        )
        outcome = run(detect_trends(session_factory, monitor_id=monitor_id, bucket=bucket, settings=settings))

        assert outcome.events_written >= 1

        async def load_event() -> TrendEvent:
            async with session_factory() as session:
                result = await session.execute(
                    select(TrendEvent).where(TrendEvent.topic_id == topic_id, TrendEvent.kind == EventKind.BURST)
                )
                return result.scalar_one()

        event = run(load_event())
        assert event.evidence_ids


class TestSustainedGrowth:
    def test_a_slow_rise_is_read_as_growth_not_a_burst(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        base = date(2026, 6, 1)
        # a gentle, steady rise -- no single day is a shock, but the trend
        # over the window is unmistakable
        counts = [10 + i for i in range(26)]
        topic_id, bucket = run(
            _seed_topic_with_series(session_factory, monitor_id, base=base, daily_counts=counts)
        )

        settings = TrendDetectionSettings(
            min_volume_floor=5, min_clean_baseline_days=14, cooldown_days=3, fdr_alpha=0.1,
            burst_z_threshold=3.5,
        )
        run(detect_trends(session_factory, monitor_id=monitor_id, bucket=bucket, settings=settings))

        async def load_events() -> list[TrendEvent]:
            async with session_factory() as session:
                result = await session.execute(select(TrendEvent).where(TrendEvent.topic_id == topic_id))
                return list(result.scalars().all())

        events = run(load_events())
        kinds = {event.kind for event in events}
        assert EventKind.SUSTAINED_GROWTH in kinds
        assert EventKind.BURST not in kinds


class TestWeekendRhythmNotReported:
    def test_a_regular_weekend_pattern_produces_no_growth_or_decline_event(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        base = date(2026, 6, 1)  # a Monday
        counts = [5 if (base + timedelta(days=i)).weekday() >= 5 else 10 for i in range(28)]
        topic_id, bucket = run(
            _seed_topic_with_series(session_factory, monitor_id, base=base, daily_counts=counts)
        )

        settings = TrendDetectionSettings(
            min_volume_floor=5, min_clean_baseline_days=14, cooldown_days=3, fdr_alpha=0.1,
        )
        run(detect_trends(session_factory, monitor_id=monitor_id, bucket=bucket, settings=settings))

        async def load_events() -> list[TrendEvent]:
            async with session_factory() as session:
                result = await session.execute(select(TrendEvent).where(TrendEvent.topic_id == topic_id))
                return list(result.scalars().all())

        events = run(load_events())
        kinds = {event.kind for event in events}
        assert EventKind.SUSTAINED_GROWTH not in kinds
        assert EventKind.DECLINE not in kinds


class TestGates:
    def test_a_topic_below_the_volume_floor_is_gated_with_a_reason(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        base = date(2026, 6, 1)
        counts = [1] * 26
        topic_id, bucket = run(
            _seed_topic_with_series(
                session_factory, monitor_id, base=base, daily_counts=counts,
                with_documents_on_last_day=False,
            )
        )

        settings = TrendDetectionSettings(min_volume_floor=5, min_clean_baseline_days=14, cooldown_days=3)
        run(detect_trends(session_factory, monitor_id=monitor_id, bucket=bucket, settings=settings))

        async def load_gate() -> GateOutcome:
            async with session_factory() as session:
                result = await session.execute(
                    select(GateOutcome).where(GateOutcome.topic_id == topic_id, GateOutcome.bucket == bucket)
                )
                return result.scalar_one()

        gate = run(load_gate())
        assert gate.passed is False
        assert gate.reason == GateReason.BELOW_VOLUME_FLOOR

    def test_a_topic_with_too_few_clean_days_is_gated_with_a_reason(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        base = date(2026, 6, 1)
        counts = [10] * 5  # far fewer than min_clean_baseline_days
        topic_id, bucket = run(
            _seed_topic_with_series(
                session_factory, monitor_id, base=base, daily_counts=counts,
                with_documents_on_last_day=False,
            )
        )

        settings = TrendDetectionSettings(min_volume_floor=5, min_clean_baseline_days=14, cooldown_days=3)
        run(detect_trends(session_factory, monitor_id=monitor_id, bucket=bucket, settings=settings))

        async def load_gate() -> GateOutcome:
            async with session_factory() as session:
                result = await session.execute(
                    select(GateOutcome).where(GateOutcome.topic_id == topic_id, GateOutcome.bucket == bucket)
                )
                return result.scalar_one()

        gate = run(load_gate())
        assert gate.passed is False
        assert gate.reason == GateReason.INSUFFICIENT_BASELINE

    def test_a_truncated_day_cannot_anchor_a_baseline(self) -> None:
        topic_id = uuid.uuid4()
        base = date(2026, 6, 1)
        points = [
            SeriesPoint(bucket=base + timedelta(days=i), value=10.0, sample_size=100, quality=PointQuality.CLEAN)
            for i in range(20)
        ]
        # a truncated day right before the tested bucket must not count
        # toward the clean baseline
        bucket = base + timedelta(days=21)
        points.append(
            SeriesPoint(bucket=base + timedelta(days=20), value=999.0, sample_size=100, quality=PointQuality.TRUNCATED)
        )
        series = Series(topic_id=topic_id, points=points)

        outcome, baseline = baseline_sufficient(series, bucket, min_clean_baseline_days=14)
        assert outcome.passed is True
        assert baseline.clean_days == 20  # the truncated point excluded, not counted


class TestMultipleComparisons:
    def test_thirty_random_candidates_yield_nothing_after_correction(self) -> None:
        import random

        random.seed(42)
        candidates = [
            Candidate(
                topic_id=uuid.uuid4(), kind=EventKind.BURST, method=DetectionMethod.ROBUST_Z,
                test_result=TestResult(
                    method=DetectionMethod.ROBUST_Z, statistic=3.5, p_value=random.uniform(0.2, 1.0),
                    observed=10.0, expected=5.0, direction=Direction.RISING,
                ),
            )
            for _ in range(30)
        ]
        survivors = apply_correction(candidates, alpha=0.1)
        assert survivors == []


class TestCooldown:
    def test_a_repeat_within_the_cooldown_window_is_suppressed(self) -> None:
        topic_id = uuid.uuid4()
        bucket = date(2026, 6, 20)
        last_dates = {EventKind.BURST: bucket - timedelta(days=1)}

        outcome = cooldown(topic_id, bucket, EventKind.BURST, last_event_dates=last_dates, cooldown_days=3)
        assert outcome.passed is False
        assert outcome.reason == GateReason.COOLDOWN_ACTIVE

    def test_a_repeat_run_of_the_same_date_is_idempotent(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        base = date(2026, 6, 1)
        counts = [10] * 25 + [50]
        topic_id, bucket = run(
            _seed_topic_with_series(session_factory, monitor_id, base=base, daily_counts=counts)
        )

        settings = TrendDetectionSettings(
            min_volume_floor=5, min_clean_baseline_days=14, cooldown_days=3, fdr_alpha=0.1,
            burst_z_threshold=3.5,
        )
        run(detect_trends(session_factory, monitor_id=monitor_id, bucket=bucket, settings=settings))
        run(detect_trends(session_factory, monitor_id=monitor_id, bucket=bucket, settings=settings))

        async def load_count() -> int:
            async with session_factory() as session:
                result = await session.execute(select(TrendEvent).where(TrendEvent.topic_id == topic_id))
                return len(result.scalars().all())

        assert run(load_count()) == 1


class TestEvidenceRequired:
    def test_an_event_is_refused_without_evidence(self, session_factory: async_sessionmaker) -> None:
        run(_make_monitor(session_factory))
        topic_id = uuid.uuid4()  # a topic that exists nowhere: no assigned documents

        async def call() -> None:
            async with session_factory() as session:
                await select_evidence(
                    session, topic_id=topic_id, bucket=date.today(), method=DetectionMethod.ROBUST_Z
                )

        with pytest.raises(NoEvidenceAvailable):
            run(call())
