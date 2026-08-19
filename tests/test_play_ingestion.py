"""Tests for Play review collection, from a recorded fixture. No network call.

Requirements: 2, 3, 4, 5, 6, 7.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any

import pytest
from sqlalchemy import pool, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.application.sources import MonitorSourceService
from mlsc.core.fetch.breaker import Breaker, BreakerSettings
from mlsc.core.fetch.cache import ResponseCache
from mlsc.core.fetch.client import FetchClient
from mlsc.core.fetch.contracts import (
    ClientProfile,
    FetchRequest,
    IllegitimatelyEmpty,
    UnexpectedContentType,
)
from mlsc.core.fetch.throttle import HostBudget, Throttle
from mlsc.core.fetch.transports import TransportResponse
from mlsc.db.models import Base, FetchStats, IngestionRun, SourceName, TargetType
from mlsc.pipeline.normalize import hash_author
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.schemas.sources import MonitorSourceCreateRequest
from mlsc.sources.play import PlayAdapter, PlayCollectionFailed, PlayCursor
from mlsc.tasks.ingest import collect_play_reviews

LOCAL_DATABASE_URL = "postgresql+asyncpg://mlsc:mlsc@localhost:55433/mlsc"

# One real review's positional shape, captured from a live com.roblox.client
# response and reused verbatim so the fixture matches what Play actually sends.
_RAW_REVIEW_TEMPLATE = [
    "{review_id}",
    ["{username}", [None, 2, None, [None, None, "https://example.test/avatar.png"]]],
    "{score}",
    None,
    "{content}",
    ["{timestamp}", 0],
    0,
    None,
    None,
    ["author-id", "{username}", None, [[None, 2, None, [None, None, None]], True], [None, 2, None, [None, None, None]]],
    "{app_version}",
    None,
    None,
    None,
    None,
    None,
    1,
]


def make_raw_review(
    *,
    review_id: str,
    username: str = "Test User",
    score: int = 4,
    content: str = "good app",
    timestamp: int = 1_700_000_000,
    app_version: str | None = "1.2.3",
) -> list[Any]:
    row = list(_RAW_REVIEW_TEMPLATE)
    row[0] = review_id
    row[1] = [username, [None, 2, None, [None, None, "https://example.test/avatar.png"]]]
    row[2] = score
    row[4] = content
    row[5] = [timestamp, 0]
    row[9] = ["author-id", username, None, [[None, 2, None, [None, None, None]], True], [None, 2, None, [None, None, None]]]
    row[10] = app_version
    return row


def make_envelope_body(
    reviews: list[list[Any]], *, continuation_token: str | None = None
) -> bytes:
    """Build a Google batchexecute envelope around ``reviews``.

    The adapter reads the continuation token from ``inner[-2][-1]``, matching
    the real response's shape.
    """
    inner: list[Any] = [reviews]
    if continuation_token is not None:
        inner = [reviews, None, [None, continuation_token]]
    inner_json = json.dumps(inner)
    outer = json.dumps([["wrb.fr", "oCPfdb", inner_json]])
    return b")]}'\n\n" + outer.encode()


class FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self._store[key] = value if isinstance(value, str) else str(value)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._store.pop(key, None)

    async def incr(self, key: str) -> int:
        value = int(self._store.get(key, 0)) + 1
        self._store[key] = str(value)
        return value


class FrozenClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class FixedRandom:
    def uniform(self, low: float, high: float) -> float:
        return 0.0


class FakeTransport:
    """Returns one queued response per call, in order. No network."""

    def __init__(self, responses: list[TransportResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def send(self, request: FetchRequest) -> TransportResponse:
        self.calls += 1
        return self._responses.pop(0)


def json_content_type() -> str:
    return "application/json; charset=utf-8"


def build_client(transport: FakeTransport) -> FetchClient:
    redis = FakeRedis()
    clock = FrozenClock()
    throttle = Throttle(redis, clock=clock, random_source=FixedRandom())
    breaker = Breaker(redis, BreakerSettings(failure_threshold=3, cooldown_seconds=60.0), clock=clock)
    cache = ResponseCache(redis, ttl_seconds=3600)
    budget = HostBudget(
        capacity=100.0, refill_rate_per_second=1000.0, jitter_low_seconds=0.0, jitter_high_seconds=0.0
    )
    return FetchClient(
        breaker=breaker,
        cache=cache,
        throttle=throttle,
        plain_transport=transport,
        impersonating_transport=transport,
        host_budget=budget,
        max_transport_retries=0,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestPlayAdapterFromFixture:
    def test_cursor_stops_before_the_matching_review(self) -> None:
        reviews = [
            make_raw_review(review_id="new-2", timestamp=2000),
            make_raw_review(review_id="new-1", timestamp=1500),
            make_raw_review(review_id="seen-already", timestamp=1000),
        ]
        transport = FakeTransport(
            [TransportResponse(200, json_content_type(), make_envelope_body(reviews), "fixture/1")]
        )
        client = build_client(transport)
        adapter = PlayAdapter(client)
        cursor = PlayCursor(last_external_id="seen-already", last_published_at=None)

        result = run(adapter.fetch("com.example.app", cursor, quota=50))

        assert [r.external_id for r in result.reviews] == ["new-2", "new-1"]
        assert result.quota_reached is False

    def test_allowance_truncates_and_reports_reached(self) -> None:
        reviews = [make_raw_review(review_id=f"r{i}", timestamp=1000 + i) for i in range(5)]
        transport = FakeTransport(
            [TransportResponse(200, json_content_type(), make_envelope_body(reviews), "fixture/1")]
        )
        client = build_client(transport)
        adapter = PlayAdapter(client)

        result = run(adapter.fetch("com.example.app", PlayCursor(), quota=3))

        assert len(result.reviews) == 3
        assert result.quota_reached is True

    def test_validation_failure_from_malformed_envelope_raises(self) -> None:
        transport = FakeTransport(
            [TransportResponse(200, json_content_type(), b"not an envelope", "fixture/1")]
        )
        client = build_client(transport)
        adapter = PlayAdapter(client)

        with pytest.raises(PlayCollectionFailed):
            run(adapter.fetch("com.example.app", PlayCursor(), quota=10))

    def test_illegitimately_empty_response_raises(self) -> None:
        transport = FakeTransport(
            [TransportResponse(200, json_content_type(), make_envelope_body([]), "fixture/1")]
        )
        client = build_client(transport)
        adapter = PlayAdapter(client)

        with pytest.raises(PlayCollectionFailed) as failure:
            run(adapter.fetch("com.example.app", PlayCursor(), quota=10))
        assert isinstance(failure.value.payload, IllegitimatelyEmpty)


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


async def _setup_monitor_and_source(session_factory: async_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
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
    source = await MonitorSourceService(session_factory).attach(
        monitor.id,
        MonitorSourceCreateRequest(
            source_name=SourceName.PLAY,
            config={"package_id": "com.roblox.client"},
            daily_quota=10,
        ),
    )
    return monitor.id, source.id


async def _create_run(
    session_factory: async_sessionmaker, monitor_id: uuid.UUID, *, is_backfill: bool = False
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            IngestionRun(
                id=run_id, monitor_id=monitor_id, run_date=date.today(), is_backfill=is_backfill
            )
        )
        await session.commit()
    return run_id


class TestCollectionTask:
    def test_duplicate_rows_are_rejected_on_a_re_run(self, session_factory: async_sessionmaker) -> None:
        monitor_id, source_id = run(_setup_monitor_and_source(session_factory))
        reviews = [make_raw_review(review_id="dup-1", timestamp=1000, username="Alice")]

        async def collect_once(*, is_backfill: bool) -> FetchStats:
            transport = FakeTransport(
                [TransportResponse(200, json_content_type(), make_envelope_body(reviews), "fixture/1")]
            )
            client = build_client(transport)
            run_id = await _create_run(session_factory, monitor_id, is_backfill=is_backfill)
            return await collect_play_reviews(
                session_factory=session_factory,
                fetch_client=client,
                run_id=run_id,
                source_id=source_id,
            )

        first = run(collect_once(is_backfill=False))
        assert first.kept == 1
        assert first.duplicates == 0

        # Reset the cursor to simulate re-running the same day's collection.
        async def reset_cursor() -> None:
            from mlsc.repositories.sources import MonitorSourceRepository

            async with session_factory() as session:
                source = await MonitorSourceRepository(session).get(source_id)
                source.last_external_id = None
                source.last_published_at = None
                await session.commit()

        run(reset_cursor())

        second = run(collect_once(is_backfill=True))
        assert second.kept == 0
        assert second.duplicates == 1

    def test_cursor_does_not_advance_after_a_validation_failure(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id, source_id = run(_setup_monitor_and_source(session_factory))
        transport = FakeTransport(
            [TransportResponse(200, json_content_type(), b"garbage", "fixture/1")]
        )
        client = build_client(transport)
        run_id = run(_create_run(session_factory, monitor_id))

        stats = run(
            collect_play_reviews(
                session_factory=session_factory,
                fetch_client=client,
                run_id=run_id,
                source_id=source_id,
            )
        )

        assert stats.validation_failed is True
        assert stats.kept == 0

        async def read_cursor() -> tuple[str | None, Any]:
            from mlsc.repositories.sources import MonitorSourceRepository

            async with session_factory() as session:
                source = await MonitorSourceRepository(session).get(source_id)
                return source.last_external_id, source.last_published_at

        last_external_id, last_published_at = run(read_cursor())
        assert last_external_id is None
        assert last_published_at is None

    def test_a_statistics_row_is_written_on_failure(self, session_factory: async_sessionmaker) -> None:
        monitor_id, source_id = run(_setup_monitor_and_source(session_factory))
        transport = FakeTransport(
            [TransportResponse(200, json_content_type(), make_envelope_body([]), "fixture/1")]
        )
        client = build_client(transport)
        run_id = run(_create_run(session_factory, monitor_id))

        stats = run(
            collect_play_reviews(
                session_factory=session_factory,
                fetch_client=client,
                run_id=run_id,
                source_id=source_id,
            )
        )

        assert stats.run_id == run_id
        assert stats.monitor_source_id == source_id
        assert stats.validation_failed is True
        assert stats.error is not None
        assert stats.library_version

    def test_author_hash_is_stored_and_no_raw_username_appears(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id, source_id = run(_setup_monitor_and_source(session_factory))
        username = "a-very-identifiable-name"
        reviews = [make_raw_review(review_id="hash-check", username=username, timestamp=1000)]
        transport = FakeTransport(
            [TransportResponse(200, json_content_type(), make_envelope_body(reviews), "fixture/1")]
        )
        client = build_client(transport)
        run_id = run(_create_run(session_factory, monitor_id))

        run(
            collect_play_reviews(
                session_factory=session_factory,
                fetch_client=client,
                run_id=run_id,
                source_id=source_id,
            )
        )

        async def load_document() -> Any:
            from sqlalchemy import select

            from mlsc.db.models import Document

            async with session_factory() as session:
                result = await session.execute(
                    select(Document).where(Document.external_id == "hash-check")
                )
                return result.scalar_one()

        document = run(load_document())
        assert document.author_hash == hash_author(username)
        assert username not in document.author_hash
        assert username not in json.dumps(document.raw)
        assert document.body is not None and username not in document.body
