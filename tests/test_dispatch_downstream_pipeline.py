"""Tests for the trend detection, scoring, alerting, and insight generation
stages `_run_downstream_pipeline` appends after `run_daily_analytics`.

Requirements: 1, 2.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import pool, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.config import ConfigurationError, TrendDetectionSettings
from mlsc.db.models import Base, IngestionRun, RunStatus, TargetType
from mlsc.llm.router import LlmRouter
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.tasks import dispatch

LOCAL_DATABASE_URL = "postgresql+asyncpg://mlsc:mlsc@localhost:55433/mlsc_test"


def run(coro):  # noqa: ANN001, ANN201
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
        pytest.skip("local Compose PostgreSQL is not reachable at localhost:55433/mlsc_test")
    run(_reset_schema(engine))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    run(engine.dispose())


class _FakeEmbedder:
    """Avoids loading sentence-transformers for a pipeline run with no
    documents to embed."""

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 384 for _ in texts]


async def _make_monitor_and_run(session_factory, run_date: date) -> tuple[uuid.UUID, uuid.UUID]:  # noqa: ANN001
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Roblox", target_type=TargetType.PRODUCT, seed={"identifiers": ["com.roblox.client"]},
            cron_expression="0 3 * * *", timezone="UTC", retention_days=90,
        )
    )
    run_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(IngestionRun(id=run_id, monitor_id=monitor.id, run_date=run_date, status=RunStatus.COMPLETE))
        await session.commit()
    return monitor.id, run_id


class TestAppendedStages:
    def test_detection_scoring_alerting_and_insights_run_in_order(
        self, session_factory, monkeypatch
    ) -> None:
        run_date = date(2026, 6, 15)
        monitor_id, run_id = run(_make_monitor_and_run(session_factory, run_date))
        calls: list[str] = []

        async def fake_detect_trends(session_factory, *, monitor_id, bucket, settings):  # noqa: ANN001
            assert bucket == run_date
            assert isinstance(settings, TrendDetectionSettings)
            calls.append("detect_trends")

        async def fake_score_trends(session_factory, *, monitor_id, bucket, settings):  # noqa: ANN001
            assert bucket == run_date
            calls.append("score_trends")

        async def fake_evaluate_alerts(session_factory, *, monitor_id, bucket):  # noqa: ANN001
            assert bucket == run_date
            calls.append("evaluate_alerts")

        async def fake_generate_insights(session_factory, *, monitor_id, period_start, period_end, llm_router):  # noqa: ANN001
            assert period_start == period_end == run_date
            calls.append("generate_insights")

        monkeypatch.setattr("mlsc.pipeline.enrich.Embedder", _FakeEmbedder)
        monkeypatch.setattr("mlsc.tasks.analytics.detect_trends", fake_detect_trends)
        monkeypatch.setattr("mlsc.tasks.analytics.score_trends", fake_score_trends)
        monkeypatch.setattr("mlsc.tasks.alerts.evaluate_alerts", fake_evaluate_alerts)
        monkeypatch.setattr("mlsc.tasks.insights.generate_insights", fake_generate_insights)
        monkeypatch.setattr(LlmRouter, "from_configuration", classmethod(lambda cls: LlmRouter({})))

        run(dispatch._run_downstream_pipeline(session_factory, run_id=run_id, monitor_id=monitor_id))

        assert calls == ["detect_trends", "score_trends", "evaluate_alerts", "generate_insights"]

    def test_unconfigured_llm_tier_skips_insights_without_crashing(
        self, session_factory, monkeypatch
    ) -> None:
        run_date = date(2026, 6, 15)
        monitor_id, run_id = run(_make_monitor_and_run(session_factory, run_date))
        calls: list[str] = []

        async def fake_detect_trends(session_factory, *, monitor_id, bucket, settings):  # noqa: ANN001
            calls.append("detect_trends")

        async def fake_score_trends(session_factory, *, monitor_id, bucket, settings):  # noqa: ANN001
            calls.append("score_trends")

        async def fake_evaluate_alerts(session_factory, *, monitor_id, bucket):  # noqa: ANN001
            calls.append("evaluate_alerts")

        async def fake_generate_insights(*args, **kwargs):  # noqa: ANN002, ANN003
            calls.append("generate_insights")

        def raise_unconfigured(cls):  # noqa: ANN001
            raise ConfigurationError("no tier configured")

        monkeypatch.setattr("mlsc.pipeline.enrich.Embedder", _FakeEmbedder)
        monkeypatch.setattr("mlsc.tasks.analytics.detect_trends", fake_detect_trends)
        monkeypatch.setattr("mlsc.tasks.analytics.score_trends", fake_score_trends)
        monkeypatch.setattr("mlsc.tasks.alerts.evaluate_alerts", fake_evaluate_alerts)
        monkeypatch.setattr("mlsc.tasks.insights.generate_insights", fake_generate_insights)
        monkeypatch.setattr(LlmRouter, "from_configuration", classmethod(raise_unconfigured))

        run(dispatch._run_downstream_pipeline(session_factory, run_id=run_id, monitor_id=monitor_id))

        assert calls == ["detect_trends", "score_trends", "evaluate_alerts"]
