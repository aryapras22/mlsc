"""Core tests for daily topic metrics. No stochastic algorithm in the loop.

Requirements: 2, 3, 4, 6, 7, 8.
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
from mlsc.application.sources import MonitorSourceService
from mlsc.db.models import (
    Assignment,
    AssignmentMethod,
    Base,
    DailyMetric,
    Document,
    Enrichment,
    FetchStats,
    IngestionRun,
    QuotaOutcome,
    SentimentLabel,
    SourceName,
    TargetType,
    Topic,
)
from mlsc.pipeline.analytics.buckets import BucketAmbiguous, bucket_for
from mlsc.pipeline.normalize import hash_author, hash_content
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.schemas.sources import MonitorSourceCreateRequest
from mlsc.tasks.analytics import recompute_affected, rollup_daily
from mlsc.db.models import RollupReason

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


async def _make_monitor(session_factory: async_sessionmaker, *, timezone: str = "UTC") -> uuid.UUID:
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Roblox", target_type=TargetType.PRODUCT, seed={"identifiers": ["x"]},
            cron_expression="0 3 * * *", timezone=timezone, retention_days=90,
        )
    )
    return monitor.id


async def _attach_play_source(session_factory: async_sessionmaker, monitor_id: uuid.UUID) -> uuid.UUID:
    source = await MonitorSourceService(session_factory).attach(
        monitor_id,
        MonitorSourceCreateRequest(
            source_name=SourceName.PLAY, config={"package_id": "com.example.app"}, daily_quota=10,
        ),
    )
    return source.id


async def _write_run_and_ledger(
    session_factory: async_sessionmaker,
    *,
    monitor_id: uuid.UUID,
    source_id: uuid.UUID,
    bucket: date,
    kept: int,
    quota_outcome: QuotaOutcome = QuotaOutcome.WITHIN_ALLOWANCE,
) -> None:
    async with session_factory() as session:
        run_id = uuid.uuid4()
        session.add(IngestionRun(id=run_id, monitor_id=monitor_id, run_date=bucket))
        await session.flush()
        session.add(
            FetchStats(
                id=uuid.uuid4(), run_id=run_id, monitor_source_id=source_id,
                attempted=kept, fetched=kept, duplicates=0, kept=kept, quota=10,
                quota_outcome=quota_outcome, validation_failed=False,
                library_version="1.0", duration_seconds=1.0, error=None,
            )
        )
        await session.commit()


async def _add_document(
    session_factory: async_sessionmaker,
    *,
    monitor_id: uuid.UUID,
    published_at: datetime,
    rating: int | None = 5,
    sentiment_score: float | None = 0.5,
    sentiment_label: SentimentLabel | None = SentimentLabel.POSITIVE,
    topic_id: uuid.UUID | None = None,
) -> uuid.UUID:
    document_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Document(
                id=document_id, monitor_id=monitor_id, source_name=SourceName.PLAY,
                external_id=str(document_id), entity_id="x", url=None,
                author_hash=hash_author("u"), body="text", published_at=published_at,
                rating=rating, app_version="1", engagement=1,
                content_hash=hash_content(str(document_id)), raw={},
            )
        )
        await session.flush()
        session.add(
            Enrichment(
                id=uuid.uuid4(), document_id=document_id, is_relevant=True, embedding=[0.1] * 384,
                sentiment_score=sentiment_score, sentiment_label=sentiment_label, model_versions={},
            )
        )
        await session.commit()
    if topic_id is not None:
        async with session_factory() as session:
            session.add(
                Assignment(
                    id=uuid.uuid4(), document_id=document_id, topic_id=topic_id,
                    similarity=0.9, method=AssignmentMethod.CENTROID,
                )
            )
            await session.commit()
    return document_id


async def _add_topic(session_factory: async_sessionmaker, monitor_id: uuid.UUID) -> uuid.UUID:
    topic_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Topic(
                id=topic_id, monitor_id=monitor_id, label="t", keywords=[], centroid=[0.1] * 384,
                doc_count=0, first_seen=date.today(), last_seen=date.today(),
            )
        )
        await session.commit()
    return topic_id


class TestPrevalenceShare:
    def test_share_equals_count_over_sample_size(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_play_source(session_factory, monitor_id))
        bucket = date(2026, 8, 10)
        run(_write_run_and_ledger(session_factory, monitor_id=monitor_id, source_id=source_id, bucket=bucket, kept=10))
        for _ in range(4):
            run(
                _add_document(
                    session_factory, monitor_id=monitor_id,
                    published_at=datetime(2026, 8, 10, 12, tzinfo=dt_timezone.utc),
                )
            )

        outcome = run(rollup_daily(session_factory, monitor_id=monitor_id, buckets=[bucket], reason=RollupReason.SCHEDULED))
        assert outcome.failed == []

        async def load() -> DailyMetric:
            async with session_factory() as session:
                result = await session.execute(
                    select(DailyMetric).where(
                        DailyMetric.monitor_id == monitor_id, DailyMetric.source_name == SourceName.PLAY,
                        DailyMetric.topic_id.is_(None),
                    )
                )
                return result.scalar_one()

        metric = run(load())
        assert metric.doc_count == 4
        assert metric.sample_size == 10
        assert metric.doc_count_share == pytest.approx(0.4)


class TestTruncatedDay:
    def test_a_truncated_day_is_flagged(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_play_source(session_factory, monitor_id))
        bucket = date(2026, 8, 11)
        run(
            _write_run_and_ledger(
                session_factory, monitor_id=monitor_id, source_id=source_id, bucket=bucket, kept=10,
                quota_outcome=QuotaOutcome.ALLOWANCE_REACHED,
            )
        )
        run(_add_document(session_factory, monitor_id=monitor_id, published_at=datetime(2026, 8, 11, 12, tzinfo=dt_timezone.utc)))

        run(rollup_daily(session_factory, monitor_id=monitor_id, buckets=[bucket], reason=RollupReason.SCHEDULED))

        async def load() -> DailyMetric:
            async with session_factory() as session:
                result = await session.execute(
                    select(DailyMetric).where(
                        DailyMetric.monitor_id == monitor_id, DailyMetric.topic_id.is_(None),
                        DailyMetric.source_name == SourceName.PLAY,
                    )
                )
                return result.scalar_one()

        metric = run(load())
        assert metric.quota_hit is True


class TestAbsentSource:
    def test_an_absent_source_produces_no_row_rather_than_a_zero(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_play_source(session_factory, monitor_id))
        bucket = date(2026, 8, 12)
        run(_write_run_and_ledger(session_factory, monitor_id=monitor_id, source_id=source_id, bucket=bucket, kept=0))

        run(rollup_daily(session_factory, monitor_id=monitor_id, buckets=[bucket], reason=RollupReason.SCHEDULED))

        async def load_count() -> int:
            async with session_factory() as session:
                result = await session.execute(
                    select(DailyMetric).where(DailyMetric.monitor_id == monitor_id)
                )
                return len(result.scalars().all())

        # No row at all is written for a bucket where the only source is
        # absent -- not a monitor-level row with doc_count=0 as a stand-in.
        assert run(load_count()) == 0


class TestNonUtcBucketBoundary:
    def test_a_bucket_boundary_in_a_non_utc_timezone(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory, timezone="Asia/Jakarta"))
        source_id = run(_attach_play_source(session_factory, monitor_id))
        # Jakarta is UTC+7: 2026-08-13 17:30 UTC is 2026-08-14 00:30 in Jakarta,
        # so this document belongs to the 14th locally despite being the 13th in UTC.
        bucket = date(2026, 8, 14)
        run(_write_run_and_ledger(session_factory, monitor_id=monitor_id, source_id=source_id, bucket=bucket, kept=1))
        run(
            _add_document(
                session_factory, monitor_id=monitor_id,
                published_at=datetime(2026, 8, 13, 17, 30, tzinfo=dt_timezone.utc),
            )
        )

        outcome = run(rollup_daily(session_factory, monitor_id=monitor_id, buckets=[bucket], reason=RollupReason.SCHEDULED))
        assert outcome.failed == []

        async def load() -> DailyMetric:
            async with session_factory() as session:
                result = await session.execute(
                    select(DailyMetric).where(
                        DailyMetric.monitor_id == monitor_id, DailyMetric.topic_id.is_(None),
                        DailyMetric.source_name == SourceName.PLAY,
                    )
                )
                return result.scalar_one()

        metric = run(load())
        assert metric.doc_count == 1


class TestNaiveTimestampRejected:
    def test_a_naive_timestamp_is_rejected(self) -> None:
        with pytest.raises(BucketAmbiguous):
            bucket_for(datetime(2026, 8, 10, 12, 0), timezone="UTC")


class TestLateArrival:
    def test_a_late_document_recomputes_only_its_own_bucket(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_play_source(session_factory, monitor_id))
        bucket1 = date(2026, 8, 15)
        bucket2 = date(2026, 8, 16)

        run(_write_run_and_ledger(session_factory, monitor_id=monitor_id, source_id=source_id, bucket=bucket1, kept=1))
        run(_add_document(session_factory, monitor_id=monitor_id, published_at=datetime(2026, 8, 15, 12, tzinfo=dt_timezone.utc)))
        run(rollup_daily(session_factory, monitor_id=monitor_id, buckets=[bucket1], reason=RollupReason.SCHEDULED))

        run(_write_run_and_ledger(session_factory, monitor_id=monitor_id, source_id=source_id, bucket=bucket2, kept=1))
        late_document_id = run(
            _add_document(session_factory, monitor_id=monitor_id, published_at=datetime(2026, 8, 16, 12, tzinfo=dt_timezone.utc))
        )

        outcome = run(recompute_affected(session_factory, monitor_id=monitor_id, document_ids=[late_document_id]))
        assert [b.bucket for b in outcome.completed] == [bucket2]

        async def load_bucket1_reason() -> RollupReason:
            async with session_factory() as session:
                result = await session.execute(
                    select(DailyMetric.reason).where(
                        DailyMetric.monitor_id == monitor_id, DailyMetric.bucket == bucket1,
                        DailyMetric.topic_id.is_(None), DailyMetric.source_name.is_(None),
                    )
                )
                return result.scalar_one()

        assert run(load_bucket1_reason()) is RollupReason.SCHEDULED


class TestPruneOnRecompute:
    def test_a_recompute_prunes_rows_no_longer_supported(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_play_source(session_factory, monitor_id))
        topic_id = run(_add_topic(session_factory, monitor_id))
        bucket = date(2026, 8, 17)

        run(_write_run_and_ledger(session_factory, monitor_id=monitor_id, source_id=source_id, bucket=bucket, kept=1))
        document_id = run(
            _add_document(
                session_factory, monitor_id=monitor_id,
                published_at=datetime(2026, 8, 17, 12, tzinfo=dt_timezone.utc), topic_id=topic_id,
            )
        )
        run(rollup_daily(session_factory, monitor_id=monitor_id, buckets=[bucket], reason=RollupReason.SCHEDULED))

        async def topic_row_count() -> int:
            async with session_factory() as session:
                result = await session.execute(
                    select(DailyMetric).where(
                        DailyMetric.monitor_id == monitor_id, DailyMetric.topic_id == topic_id
                    )
                )
                return len(result.scalars().all())

        # Two rows reference this topic: the (source, topic) breakdown and
        # the (all-sources, topic) aggregate.
        assert run(topic_row_count()) == 2

        # Remove the assignment (as a merge or retention would) and recompute.
        async def remove_assignment() -> None:
            async with session_factory() as session:
                result = await session.execute(
                    select(Assignment).where(Assignment.document_id == document_id)
                )
                assignment = result.scalar_one()
                await session.delete(assignment)
                await session.commit()

        run(remove_assignment())
        run(rollup_daily(session_factory, monitor_id=monitor_id, buckets=[bucket], reason=RollupReason.SCHEDULED))

        assert run(topic_row_count()) == 0


class TestMergedTopicResolves:
    def test_a_merged_topics_figures_remain_queryable_by_identifier(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_play_source(session_factory, monitor_id))
        topic_id = run(_add_topic(session_factory, monitor_id))
        bucket = date(2026, 8, 18)

        run(_write_run_and_ledger(session_factory, monitor_id=monitor_id, source_id=source_id, bucket=bucket, kept=1))
        run(
            _add_document(
                session_factory, monitor_id=monitor_id,
                published_at=datetime(2026, 8, 18, 12, tzinfo=dt_timezone.utc), topic_id=topic_id,
            )
        )
        run(rollup_daily(session_factory, monitor_id=monitor_id, buckets=[bucket], reason=RollupReason.SCHEDULED))

        # Simulate a merge: the topic row itself is marked merged, but no
        # DailyMetric row is rewritten -- persistent-topics never deletes a
        # topic, so a lookup by the absorbed identifier still resolves.
        async def mark_merged() -> None:
            async with session_factory() as session:
                topic = await session.get(Topic, topic_id)
                from mlsc.db.models import TopicStatus
                topic.status = TopicStatus.MERGED
                await session.commit()

        run(mark_merged())

        from mlsc.repositories.metrics import MetricRepository

        async def load_for_topic() -> list[DailyMetric]:
            async with session_factory() as session:
                return await MetricRepository(session).for_topic(topic_id)

        rows = run(load_for_topic())
        # Two rows reference this topic: the (source, topic) breakdown and
        # the (all-sources, topic) aggregate, both correctly resolvable
        # after the topic itself is marked merged.
        assert len(rows) == 2
        assert all(row.doc_count == 1 for row in rows)
