"""Core tests for the metrics read API's application layer. No stochastic
algorithm in the loop.

Requirements: 2, 3, 4, 5, 6, 8, 9, 10.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import pool, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.alerts import AlertRuleService, RuleConflict
from mlsc.application import metrics_view
from mlsc.application.filters import FilterUnknown, resolve_topic
from mlsc.application.monitors import MonitorService
from mlsc.application.runs import RunService, SourceOutcome, SourceResult
from mlsc.application.sources import MonitorSourceService
from mlsc.core.fetch.webhook import WebhookUnreachable
from mlsc.core.locks import RunLock
from mlsc.db.models import (
    Base,
    Channel,
    DailyMetric,
    Delivery,
    DeliveryState,
    DetectionMethod,
    EventKind,
    ReadAlertKind,
    RollupReason,
    RunStatus,
    SourceName,
    TargetType,
    Topic,
    TopicStatus,
    TrendEvent,
)
from mlsc.schemas.alerts import AlertRuleCreateRequest
from mlsc.schemas.metrics import Metric
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.schemas.sources import MonitorSourceCreateRequest
from mlsc.tasks.alerts import deliver_pending, evaluate_alerts

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


_BASE = date(2026, 8, 1)


async def _seed_topic_with_metrics(
    session_factory: async_sessionmaker, monitor_id: uuid.UUID, *, quota_hit_on: date | None = None
) -> uuid.UUID:
    topic_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Topic(
                id=topic_id, monitor_id=monitor_id, label="t", keywords=[], centroid=[0.1] * 384,
                doc_count=0, first_seen=_BASE, last_seen=_BASE,
            )
        )
        await session.flush()
        for offset in range(3):
            bucket = _BASE + timedelta(days=offset)
            session.add(
                DailyMetric(
                    id=uuid.uuid4(), monitor_id=monitor_id, bucket=bucket, source_name=None, topic_id=None,
                    doc_count=10, doc_count_share=1.0, sample_size=10, quota_hit=(bucket == quota_hit_on),
                    sentiment_mean=0.1, reason=RollupReason.SCHEDULED,
                )
            )
            session.add(
                DailyMetric(
                    id=uuid.uuid4(), monitor_id=monitor_id, bucket=bucket, source_name=None, topic_id=topic_id,
                    doc_count=10, doc_count_share=1.0, sample_size=10, quota_hit=(bucket == quota_hit_on),
                    sentiment_mean=-0.2, reason=RollupReason.SCHEDULED,
                )
            )
        await session.commit()
    return topic_id


class TestDataQuality:
    def test_every_metric_response_carries_a_quality_block(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        run(_seed_topic_with_metrics(session_factory, monitor_id))

        async def load():
            async with session_factory() as session:
                overview = await metrics_view.build_overview(
                    session, monitor_id=monitor_id, start=_BASE, end=_BASE + timedelta(days=2)
                )
                ranking = await metrics_view.build_ranking(
                    session, monitor_id=monitor_id, start=_BASE, end=_BASE + timedelta(days=2)
                )
            return overview, ranking

        overview, ranking = run(load())
        assert overview.data_quality.sample_size == 30
        assert ranking.data_quality.sample_size == 30

    def test_a_truncated_day_is_marked_rather_than_an_ordinary_point(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        quota_day = _BASE + timedelta(days=1)
        topic_id = run(_seed_topic_with_metrics(session_factory, monitor_id, quota_hit_on=quota_day))

        async def load():
            async with session_factory() as session:
                return await metrics_view.build_series(
                    session, monitor_id=monitor_id, metric=Metric.VOLUME, start=_BASE,
                    end=_BASE + timedelta(days=2), topic_id=topic_id, source=None,
                )

        series = run(load())
        from mlsc.schemas.metrics import PointQuality

        truncated = [point for point in series.points if point.bucket == quota_day]
        assert truncated and truncated[0].quality is PointQuality.TRUNCATED
        clean = [point for point in series.points if point.bucket != quota_day]
        assert all(point.quality is PointQuality.CLEAN for point in clean)
        assert quota_day in series.data_quality.truncated_days


class TestAbsenceAndFilterResolution:
    def test_a_merged_topic_resolves_through_lineage(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        survivor_id = uuid.uuid4()
        merged_id = uuid.uuid4()

        async def seed():
            async with session_factory() as session:
                session.add(
                    Topic(
                        id=survivor_id, monitor_id=monitor_id, label="s", keywords=[], centroid=[0.1] * 384,
                        doc_count=0, first_seen=_BASE, last_seen=_BASE,
                    )
                )
                session.add(
                    Topic(
                        id=merged_id, monitor_id=monitor_id, label="m", keywords=[], centroid=[0.1] * 384,
                        doc_count=0, first_seen=_BASE, last_seen=_BASE,
                        status=TopicStatus.MERGED, merged_into=survivor_id,
                    )
                )
                await session.commit()

        run(seed())

        async def resolve():
            async with session_factory() as session:
                return await resolve_topic(session, monitor_id=monitor_id, topic_id=merged_id)

        assert run(resolve()) == survivor_id

    def test_an_unknown_topic_is_a_named_failure_not_an_empty_result(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))

        async def resolve():
            async with session_factory() as session:
                await resolve_topic(session, monitor_id=monitor_id, topic_id=uuid.uuid4())

        with pytest.raises(FilterUnknown):
            run(resolve())


class TestRunTrigger:
    def test_a_run_trigger_does_no_collection_work_itself(self, session_factory: async_sessionmaker) -> None:
        """Requirement 4/5: starting a run returns an id and does no work in
        the request — finalisation is a separate call the caller controls."""
        monitor_id = run(_make_monitor(session_factory))
        run(
            MonitorSourceService(session_factory).attach(
                monitor_id,
                MonitorSourceCreateRequest(
                    source_name=SourceName.PLAY,
                    config={"package_id": "com.roblox.client"},
                    daily_quota=10,
                ),
            )
        )

        class NullDispatcher:
            def __init__(self) -> None:
                self.calls: list[uuid.UUID] = []

            def dispatch_run(self, run_id: uuid.UUID) -> None:
                self.calls.append(run_id)

        class FakeRedis:
            def __init__(self) -> None:
                self._store: dict[str, str] = {}

            async def set(self, key, value, nx=False, ex=None):  # noqa: ANN001
                if nx and key in self._store:
                    return False
                self._store[key] = value
                return True

            async def get(self, key):  # noqa: ANN001
                return self._store.get(key)

            async def eval(self, script, numkeys, key, token):  # noqa: ANN001
                if self._store.get(key) == token:
                    del self._store[key]
                    return 1
                return 0

        dispatcher = NullDispatcher()
        service = RunService(session_factory, RunLock(FakeRedis()), dispatcher)

        run_id = run(service.start(monitor_id, date.today()))

        assert dispatcher.calls == [run_id]  # the only thing that happened is an enqueue


class TestAlerts:
    async def _seed_event(self, session_factory: async_sessionmaker, monitor_id: uuid.UUID) -> uuid.UUID:
        topic_id = uuid.uuid4()
        async with session_factory() as session:
            session.add(
                Topic(
                    id=topic_id, monitor_id=monitor_id, label="t", keywords=[], centroid=[0.1] * 384,
                    doc_count=0, first_seen=_BASE, last_seen=_BASE,
                )
            )
            await session.flush()
            session.add(
                TrendEvent(
                    id=uuid.uuid4(), monitor_id=monitor_id, topic_id=topic_id, detected_on=_BASE,
                    kind=EventKind.BURST, method=DetectionMethod.ROBUST_Z, severity=4.0,
                    statistics={}, evidence_ids=[],
                )
            )
            await session.commit()
        return topic_id

    def test_a_scraper_rule_does_not_match_a_product_event(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        run(self._seed_event(session_factory, monitor_id))

        service = AlertRuleService(session_factory)
        run(
            service.create(
                monitor_id,
                AlertRuleCreateRequest(
                    kind=ReadAlertKind.SCRAPER, conditions={}, channel=Channel.WEBHOOK,
                    target="https://example.test/hook",
                ),
            )
        )

        created = run(evaluate_alerts(session_factory, monitor_id=monitor_id, bucket=_BASE))
        assert created == 0

    def test_a_matching_product_rule_creates_one_delivery(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        run(self._seed_event(session_factory, monitor_id))

        service = AlertRuleService(session_factory)
        run(
            service.create(
                monitor_id,
                AlertRuleCreateRequest(
                    kind=ReadAlertKind.PRODUCT, conditions={"event_kind": "burst"},
                    channel=Channel.WEBHOOK, target="https://example.test/hook",
                ),
            )
        )

        created = run(evaluate_alerts(session_factory, monitor_id=monitor_id, bucket=_BASE))
        assert created == 1

    def test_creating_an_equivalent_rule_twice_is_rejected(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        service = AlertRuleService(session_factory)
        request = AlertRuleCreateRequest(
            kind=ReadAlertKind.PRODUCT, conditions={"event_kind": "burst"},
            channel=Channel.WEBHOOK, target="https://example.test/hook",
        )
        run(service.create(monitor_id, request))
        with pytest.raises(RuleConflict):
            run(service.create(monitor_id, request))

    def test_a_failed_delivery_is_retried_then_abandoned_and_stays_visible(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        run(self._seed_event(session_factory, monitor_id))

        service = AlertRuleService(session_factory)
        run(
            service.create(
                monitor_id,
                AlertRuleCreateRequest(
                    kind=ReadAlertKind.PRODUCT, conditions={"event_kind": "burst"},
                    channel=Channel.WEBHOOK, target="https://example.test/hook",
                ),
            )
        )
        run(evaluate_alerts(session_factory, monitor_id=monitor_id, bucket=_BASE))

        class FailingSender:
            async def send(self, url: str, payload: dict) -> None:
                raise WebhookUnreachable("simulated failure")

        for _attempt in range(3):
            run(deliver_pending(session_factory, sender=FailingSender()))

        async def load_delivery() -> Delivery:
            async with session_factory() as session:
                result = await session.execute(select(Delivery))
                return result.scalars().one()

        delivery = run(load_delivery())
        assert delivery.state == DeliveryState.ABANDONED
        assert delivery.attempts == 3
        assert delivery.last_error == "simulated failure"  # visible, not lost
