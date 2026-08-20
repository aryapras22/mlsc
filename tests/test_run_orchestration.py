"""Core tests for run orchestration: one run per date, partial classification,
and the all-empty-run failure. Uses a synchronous dispatcher; no broker.

Requirements: 1, 5, 7.
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
from mlsc.application.runs import (
    RunAlreadyActive,
    RunService,
    SourceOutcome,
    SourceResult,
)
from mlsc.core.locks import RunLock
from mlsc.db.models import Base, MonitorStatus, RunStatus, TargetType
from mlsc.schemas.monitors import MonitorCreateRequest

LOCAL_DATABASE_URL = "postgresql+asyncpg://mlsc:mlsc@localhost:55433/mlsc"


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


class NullDispatcher:
    def dispatch_run(self, run_id: uuid.UUID) -> None:
        pass


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


@pytest.fixture
def run_service(session_factory):  # noqa: ANN001
    return RunService(session_factory, RunLock(FakeRedis()), NullDispatcher())


async def _make_monitor(session_factory) -> uuid.UUID:  # noqa: ANN001
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
    return monitor.id


class TestSingleRunPerDate:
    def test_second_trigger_while_running_returns_the_same_run(
        self, session_factory, run_service
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))

        first_run_id = run(run_service.start(monitor_id, date.today()))
        with pytest.raises(RunAlreadyActive) as active:
            run(run_service.start(monitor_id, date.today()))

        assert active.value.run_id == first_run_id


class TestFinalisation:
    def test_partial_when_one_source_fails_and_one_succeeds(
        self, session_factory, run_service
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        run_id = run(run_service.start(monitor_id, date.today()))

        outcomes = [
            SourceOutcome(source_id=uuid.uuid4(), source_name="play", result=SourceResult.COLLECTED, kept=5),
            SourceOutcome(
                source_id=uuid.uuid4(),
                source_name="play",
                result=SourceResult.FAILED_VALIDATION,
                error="bad shape",
            ),
        ]

        status_ = run(run_service.finalise(run_id, outcomes, expected_volume=True))

        assert status_ is RunStatus.PARTIAL

    def test_all_sources_empty_fails_the_run(self, session_factory, run_service) -> None:
        monitor_id = run(_make_monitor(session_factory))
        run_id = run(run_service.start(monitor_id, date.today()))

        outcomes = [
            SourceOutcome(source_id=uuid.uuid4(), source_name="play", result=SourceResult.COLLECTED, kept=0),
        ]

        status_ = run(run_service.finalise(run_id, outcomes, expected_volume=True))

        assert status_ is RunStatus.FAILED

    def test_all_succeed_completes(self, session_factory, run_service) -> None:
        monitor_id = run(_make_monitor(session_factory))
        run_id = run(run_service.start(monitor_id, date.today()))

        outcomes = [
            SourceOutcome(source_id=uuid.uuid4(), source_name="play", result=SourceResult.COLLECTED, kept=5),
        ]

        status_ = run(run_service.finalise(run_id, outcomes, expected_volume=True))

        assert status_ is RunStatus.COMPLETE
