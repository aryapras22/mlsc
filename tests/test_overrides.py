"""Tests for operator-initiated repair overrides.

Requirements: 4, 6.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import pool, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.application.overrides import OverrideOverlaps, OverrideService, preview_token
from mlsc.db.models import Base, Document, OverrideKind, SourceName, TargetType
from mlsc.pipeline.normalize import hash_author, hash_content
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.schemas.overrides import OverrideRequest

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


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []

    def dispatch_override(self, job_id: uuid.UUID) -> None:
        self.dispatched.append(job_id)


class _FixedClock:
    def __init__(self, today: date) -> None:
        self._today = today

    def today(self) -> date:
        return self._today


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


async def _make_monitor(session_factory, retention_days: int = 90) -> uuid.UUID:  # noqa: ANN001
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Roblox", target_type=TargetType.PRODUCT,
            seed={"identifiers": ["com.roblox.client"]},
            cron_expression="0 3 * * *", timezone="UTC", retention_days=retention_days,
        )
    )
    return monitor.id


async def _add_document(session_factory, monitor_id: uuid.UUID, published_at: date) -> None:  # noqa: ANN001
    async with session_factory() as session:
        session.add(Document(
            id=uuid.uuid4(), monitor_id=monitor_id, source_name=SourceName.PLAY,
            external_id=str(uuid.uuid4()), entity_id="x", url=None,
            author_hash=hash_author("a"), body="body",
            published_at=published_at,
            rating=3, app_version=None, engagement=None,
            content_hash=hash_content(str(uuid.uuid4())), raw={},
        ))
        await session.commit()


class TestOverrideOverlap:
    def test_second_submission_of_same_kind_is_refused(self, session_factory) -> None:
        monitor_id = run(_make_monitor(session_factory))
        service = OverrideService(session_factory, _RecordingDispatcher())
        token = run(service.preview_retention(monitor_id)).token
        request = OverrideRequest(kind=OverrideKind.RETENTION_PURGE, purge_token=token)

        run(service.submit(monitor_id, request))

        with pytest.raises(OverrideOverlaps):
            run(service.submit(monitor_id, request))


class TestRetentionPreview:
    def test_counts_only_documents_past_the_cutoff(self, session_factory) -> None:
        monitor_id = run(_make_monitor(session_factory, retention_days=90))
        run(_add_document(session_factory, monitor_id, date.today() - timedelta(days=200)))
        run(_add_document(session_factory, monitor_id, date.today() - timedelta(days=10)))

        service = OverrideService(session_factory, _RecordingDispatcher())
        preview = run(service.preview_retention(monitor_id))

        assert preview.count == 1

    def test_token_matches_the_cutoff_and_count_it_was_issued_for(self, session_factory) -> None:
        monitor_id = run(_make_monitor(session_factory, retention_days=90))
        run(_add_document(session_factory, monitor_id, date.today() - timedelta(days=200)))

        clock = _FixedClock(date.today())
        service = OverrideService(session_factory, _RecordingDispatcher(), clock=clock)
        preview = run(service.preview_retention(monitor_id))

        cutoff = clock.today() - timedelta(days=90)
        assert preview.token == preview_token(monitor_id, cutoff, 1)

    def test_a_later_call_with_more_documents_yields_a_different_token(self, session_factory) -> None:
        monitor_id = run(_make_monitor(session_factory, retention_days=90))
        run(_add_document(session_factory, monitor_id, date.today() - timedelta(days=200)))

        service = OverrideService(session_factory, _RecordingDispatcher())
        first = run(service.preview_retention(monitor_id))

        run(_add_document(session_factory, monitor_id, date.today() - timedelta(days=201)))
        second = run(service.preview_retention(monitor_id))

        assert first.token != second.token
