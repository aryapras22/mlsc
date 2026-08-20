"""Core tests for source health, backfill, and retention.

Requirements: 1, 2, 3, 6, 7, 8.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import pool, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.backfill import BackfillOverlaps, BackfillService
from mlsc.application.health import evaluate
from mlsc.application.monitors import MonitorService
from mlsc.application.sources import MonitorSourceService
from mlsc.db.models import Base, Document, FetchStats, IngestionRun, SourceName, SourceState, TargetType
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.schemas.sources import MonitorSourceCreateRequest
from mlsc.tasks.maintenance import evaluate_source_health
from mlsc.tasks.retention import enforce_retention

LOCAL_DATABASE_URL = "postgresql+asyncpg://mlsc:mlsc@localhost:55433/mlsc"


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


async def _reachable(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except (OperationalError, OSError):
        return False


@pytest.fixture
def session_factory():
    engine = create_async_engine(LOCAL_DATABASE_URL, poolclass=pool.NullPool)
    if not run(_reachable(engine)):
        run(engine.dispose())
        pytest.skip("local Compose PostgreSQL is not reachable at localhost:55433")

    async def reset():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    run(reset())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    run(engine.dispose())


class TestHealthVerdictRule:
    def test_too_little_history_is_healthy_and_raises_nothing(self) -> None:
        verdict = evaluate(
            kept=0, previously_had_rows=True, consecutive_empty=1,
            rows_median_28d=None, history_count=2,
        )
        assert verdict.state is SourceState.HEALTHY

    def test_second_consecutive_empty_run_is_broken(self) -> None:
        verdict = evaluate(
            kept=0, previously_had_rows=True, consecutive_empty=1,
            rows_median_28d=10.0, history_count=10,
        )
        assert verdict.state is SourceState.BROKEN

    def test_volume_far_below_median_is_degraded(self) -> None:
        verdict = evaluate(
            kept=2, previously_had_rows=True, consecutive_empty=0,
            rows_median_28d=20.0, history_count=10,
        )
        assert verdict.state is SourceState.DEGRADED

    def test_normal_volume_is_healthy(self) -> None:
        verdict = evaluate(
            kept=18, previously_had_rows=True, consecutive_empty=0,
            rows_median_28d=20.0, history_count=10,
        )
        assert verdict.state is SourceState.HEALTHY


async def _make_monitor_and_source(session_factory) -> tuple[uuid.UUID, uuid.UUID]:  # noqa: ANN001
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Roblox", target_type=TargetType.PRODUCT,
            seed={"identifiers": ["com.roblox.client"]},
            cron_expression="0 3 * * *", timezone="UTC", retention_days=90,
        )
    )
    source = await MonitorSourceService(session_factory).attach(
        monitor.id,
        MonitorSourceCreateRequest(
            source_name=SourceName.PLAY, config={"package_id": "com.roblox.client"}, daily_quota=50,
        ),
    )
    return monitor.id, source.id


class TestHealthEvaluationIntegration:
    def test_alert_raised_on_second_consecutive_empty_run(self, session_factory) -> None:
        monitor_id, source_id = run(_make_monitor_and_source(session_factory))

        day_counter = {"n": 0}

        async def write_stats_and_evaluate(kept: int) -> uuid.UUID:
            run_id = uuid.uuid4()
            day_counter["n"] += 1
            run_date = date.today() - timedelta(days=30 - day_counter["n"])
            async with session_factory() as session:
                session.add(IngestionRun(id=run_id, monitor_id=monitor_id, run_date=run_date))
                await session.flush()
                session.add(FetchStats(
                    id=uuid.uuid4(), run_id=run_id, monitor_source_id=source_id,
                    attempted=kept, fetched=kept, kept=kept, quota=50,
                    library_version="1.0",
                ))
                await session.commit()
            await evaluate_source_health(session_factory, run_id)
            return run_id

        # Build up history with rows so previously_had_rows is true and median exists.
        for _ in range(6):
            run(write_stats_and_evaluate(10))
        run(write_stats_and_evaluate(0))
        run(write_stats_and_evaluate(0))

        async def read_alerts():
            from sqlalchemy import select
            from mlsc.db.models import ScraperAlert
            async with session_factory() as session:
                result = await session.execute(select(ScraperAlert))
                return list(result.scalars().all())

        alerts = run(read_alerts())
        assert len(alerts) >= 1


class TestBackfill:
    def test_overlapping_backfill_is_rejected(self, session_factory) -> None:
        monitor_id, _ = run(_make_monitor_and_source(session_factory))
        service = BackfillService(session_factory)

        run(service.submit(monitor_id, date.today() - timedelta(days=10), date.today() - timedelta(days=1)))

        with pytest.raises(BackfillOverlaps):
            run(service.submit(monitor_id, date.today() - timedelta(days=5), date.today() - timedelta(days=1)))

    def test_backfill_creates_separate_runs_from_daily(self, session_factory) -> None:
        monitor_id, _ = run(_make_monitor_and_source(session_factory))
        service = BackfillService(session_factory)

        job_id = run(service.submit(monitor_id, date.today() - timedelta(days=2), date.today() - timedelta(days=1)))

        async def noop_collect(run_id, source_id):  # noqa: ANN001
            return None

        run(service.run(job_id, collect_one_date=noop_collect))

        async def count_backfill_runs():
            from sqlalchemy import select, func
            async with session_factory() as session:
                result = await session.execute(
                    select(func.count()).select_from(IngestionRun).where(
                        IngestionRun.monitor_id == monitor_id, IngestionRun.is_backfill.is_(True)
                    )
                )
                return result.scalar_one()

        assert run(count_backfill_runs()) == 2


class TestRetention:
    def test_expired_documents_are_removed(self, session_factory) -> None:
        monitor_id, _ = run(_make_monitor_and_source(session_factory))

        async def add_old_document():
            from mlsc.pipeline.normalize import hash_author, hash_content
            async with session_factory() as session:
                session.add(Document(
                    id=uuid.uuid4(), monitor_id=monitor_id, source_name=SourceName.PLAY,
                    external_id="old-1", entity_id="x", url=None,
                    author_hash=hash_author("a"), body="old",
                    published_at=date.today() - timedelta(days=200),
                    rating=3, app_version=None, engagement=None,
                    content_hash=hash_content("old"), raw={},
                ))
                await session.commit()

        run(add_old_document())

        outcome = run(enforce_retention(session_factory, monitor_id))

        assert outcome.documents_removed == 1
