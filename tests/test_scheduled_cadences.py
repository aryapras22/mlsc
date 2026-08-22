"""Tests for the scheduled-cadences fan-out helper and its wrapping tasks.

Requirements: 8, 9.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import pool, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.db.models import Base, MonitorStatus, TargetType
from mlsc.schemas.monitors import MonitorCreateRequest
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
