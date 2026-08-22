"""Tests for the scheduled-cadences fan-out helper and its wrapping tasks.

Requirements: 8, 9.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import pool, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.db.models import Base, MonitorStatus, TargetType
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.tasks import scheduled
from mlsc.tasks.scheduled import _for_each_active_monitor

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


async def _make_monitor(session_factory, *, status: MonitorStatus) -> uuid.UUID:  # noqa: ANN001
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Roblox",
            target_type=TargetType.PRODUCT,
            seed={"identifiers": ["com.roblox.client"]},
            cron_expression="0 3 * * *",
            timezone="UTC",
            retention_days=90,
        )
    )
    if status is not MonitorStatus.ACTIVE:
        from mlsc.db.models import Monitor

        async with session_factory() as session:
            row = await session.get(Monitor, monitor.id)
            row.status = status
            await session.commit()
    return monitor.id


class TestForEachActiveMonitor:
    def test_skips_paused_monitor(self, session_factory) -> None:  # noqa: ANN001
        active_id = run(_make_monitor(session_factory, status=MonitorStatus.ACTIVE))
        run(_make_monitor(session_factory, status=MonitorStatus.PAUSED))

        called: list[uuid.UUID] = []

        async def work(monitor_id: uuid.UUID) -> None:
            called.append(monitor_id)

        run(_for_each_active_monitor(session_factory, work))

        assert called == [active_id]


class TestRunWeeklyDiscovery:
    def test_discovers_then_marks_dormant_per_active_monitor(
        self, session_factory, monkeypatch
    ) -> None:  # noqa: ANN001
        from mlsc.config import ConfigurationError

        active_id = run(_make_monitor(session_factory, status=MonitorStatus.ACTIVE))
        run(_make_monitor(session_factory, status=MonitorStatus.PAUSED))

        calls: list[tuple[str, uuid.UUID]] = []

        async def fake_discover_topics(_session_factory, *, monitor_id, **_kwargs):
            calls.append(("discover_topics", monitor_id))

        async def fake_mark_dormant_topics(_session_factory, *, monitor_id, **_kwargs):
            calls.append(("mark_dormant_topics", monitor_id))

        def raise_unconfigured():
            raise ConfigurationError("no tier configured")

        # The task builds its own engine/session factory from settings; the
        # test substitutes the fixture's factory rather than reaching a real
        # managed Postgres endpoint from configuration.
        stub_settings = SimpleNamespace(postgres=None)
        monkeypatch.setattr("mlsc.config.load_settings", lambda: stub_settings)
        monkeypatch.setattr("mlsc.db.session.build_engine", lambda _postgres: object())
        monkeypatch.setattr(
            "mlsc.db.session.build_session_factory", lambda _engine: session_factory
        )
        monkeypatch.setattr(
            "mlsc.llm.router.LlmRouter.from_configuration", classmethod(lambda cls: raise_unconfigured())
        )
        monkeypatch.setattr("mlsc.tasks.topics.discover_topics", fake_discover_topics)
        monkeypatch.setattr("mlsc.tasks.topics.mark_dormant_topics", fake_mark_dormant_topics)

        scheduled.run_weekly_discovery()

        assert calls == [
            ("discover_topics", active_id),
            ("mark_dormant_topics", active_id),
        ]


class TestRunMonthlyRefit:
    def test_refits_registry_per_active_monitor(
        self, session_factory, monkeypatch
    ) -> None:  # noqa: ANN001
        active_id = run(_make_monitor(session_factory, status=MonitorStatus.ACTIVE))
        run(_make_monitor(session_factory, status=MonitorStatus.PAUSED))

        calls: list[uuid.UUID] = []

        async def fake_refit_registry(_session_factory, *, monitor_id, **_kwargs):
            calls.append(monitor_id)

        # The task builds its own engine/session factory from settings; the
        # test substitutes the fixture's factory rather than reaching a real
        # managed Postgres endpoint from configuration.
        stub_settings = SimpleNamespace(postgres=None)
        monkeypatch.setattr("mlsc.config.load_settings", lambda: stub_settings)
        monkeypatch.setattr("mlsc.db.session.build_engine", lambda _postgres: object())
        monkeypatch.setattr(
            "mlsc.db.session.build_session_factory", lambda _engine: session_factory
        )
        monkeypatch.setattr("mlsc.pipeline.topics.refit.refit_registry", fake_refit_registry)

        scheduled.run_monthly_refit()

        assert calls == [active_id]


class TestRunRetentionSweep:
    def test_enforces_retention_per_active_monitor(
        self, session_factory, monkeypatch
    ) -> None:  # noqa: ANN001
        active_id = run(_make_monitor(session_factory, status=MonitorStatus.ACTIVE))
        run(_make_monitor(session_factory, status=MonitorStatus.PAUSED))

        calls: list[uuid.UUID] = []

        async def fake_enforce_retention(_session_factory, monitor_id):
            calls.append(monitor_id)

        # The task builds its own engine/session factory from settings; the
        # test substitutes the fixture's factory rather than reaching a real
        # managed Postgres endpoint from configuration.
        stub_settings = SimpleNamespace(postgres=None)
        monkeypatch.setattr("mlsc.config.load_settings", lambda: stub_settings)
        monkeypatch.setattr("mlsc.db.session.build_engine", lambda _postgres: object())
        monkeypatch.setattr(
            "mlsc.db.session.build_session_factory", lambda _engine: session_factory
        )
        monkeypatch.setattr("mlsc.tasks.retention.enforce_retention", fake_enforce_retention)

        scheduled.run_retention_sweep()

        assert calls == [active_id]
