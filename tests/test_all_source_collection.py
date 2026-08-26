"""Tests for collecting from every source kind: per-kind normalization, the
ledger row, the cursor, and partial failure. No network call.

Requirements: 3, 4, 5, 6, 7, 8.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from typing import Any

import pytest
from sqlalchemy import func, pool, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.application.runs import RunService
from mlsc.application.sources import MonitorSourceService
from mlsc.core.fetch.breaker import Breaker, BreakerSettings
from mlsc.core.fetch.cache import ResponseCache
from mlsc.core.fetch.client import FetchClient
from mlsc.core.fetch.contracts import FetchRequest
from mlsc.core.fetch.throttle import HostBudget, Throttle
from mlsc.core.fetch.transports import TransportResponse
from mlsc.core.locks import RunLock
from mlsc.db.models import (
    Base,
    FetchStats,
    IngestionRun,
    MonitorSource,
    RunStatus,
    SourceName,
    TargetType,
)
from mlsc.repositories.documents import DocumentRepository
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.schemas.sources import MonitorSourceCreateRequest
from mlsc.sources.appstore import AppStoreCursor, AppStoreReview
from mlsc.sources.appstore import CollectionResult as AppStoreResult
from mlsc.sources.collect import items_from
from mlsc.sources.discourse import CollectionResult as DiscourseResult
from mlsc.sources.discourse import DiscourseCursor, DiscoursePost
from mlsc.sources.hackernews import CollectionResult as HackerNewsResult
from mlsc.sources.hackernews import HackerNewsCursor, HackerNewsItem
from mlsc.sources.news.adapter import CollectionResult as NewsResult
from mlsc.sources.news.adapter import NewsArticle, NewsCursor
from mlsc.sources.play import CollectionResult as PlayResult
from mlsc.sources.play import PlayCursor, PlayReview
from mlsc.sources.rss import CollectionResult as FeedResult
from mlsc.sources.rss import FeedCursor, FeedItem
from mlsc.tasks import dispatch, ingest
from mlsc.tasks.ingest import collect_source

LOCAL_DATABASE_URL = "postgresql+asyncpg://mlsc:mlsc@localhost:55433/mlsc_test"

_PUBLISHED_AT = datetime(2026, 8, 19, tzinfo=timezone.utc)

# The handle collect.py falls back to when an item's URL yields no host. A host
# can contain neither a colon nor a space, so it cannot collide with a real one.
_UNKNOWN_OUTLET = "outlet:unknown"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def play_payload(**overrides: Any) -> PlayResult:
    fields = dict(
        external_id="play-1",
        username="Alice",
        content="good app",
        rating=4,
        published_at=_PUBLISHED_AT,
        app_version="1.2.3",
    )
    return PlayResult(
        reviews=[PlayReview(**{**fields, **overrides})],
        new_cursor=PlayCursor(),
        quota_reached=False,
    )


def appstore_payload(**overrides: Any) -> AppStoreResult:
    fields = dict(
        external_id="appstore-1",
        username="Bob",
        content="crashes often",
        rating=2,
        published_at=_PUBLISHED_AT,
        app_version="9.9",
    )
    return AppStoreResult(
        reviews=[AppStoreReview(**{**fields, **overrides})],
        new_cursor=AppStoreCursor(),
        quota_reached=False,
    )


def discourse_payload() -> DiscourseResult:
    return DiscourseResult(
        posts=[
            DiscoursePost(
                external_id="post-1",
                username="Carol",
                content="the sync keeps stalling",
                published_at=_PUBLISHED_AT,
                engagement=7,
            )
        ],
        new_cursor=DiscourseCursor(),
        quota_reached=False,
    )


def news_payload(resolved_url: str = "https://publisher.test/article") -> NewsResult:
    return NewsResult(
        articles=[
            NewsArticle(
                external_id="article-1",
                resolved_url=resolved_url,
                title="Hiking app raises prices",
                text="the extracted article body",
                published_at=_PUBLISHED_AT,
            )
        ],
        new_cursor=NewsCursor(),
        quota_reached=False,
        filtered=0,
    )


def feed_payload(*urls: str) -> FeedResult:
    return FeedResult(
        items=[
            FeedItem(
                external_id=f"entry-{index}",
                title=f"Entry {index}",
                content=f"summary {index}",
                published_at=_PUBLISHED_AT,
                url=url,
            )
            for index, url in enumerate(urls)
        ],
        new_cursor=FeedCursor(),
        quota_reached=False,
    )


def hackernews_payload() -> HackerNewsResult:
    return HackerNewsResult(
        items=[
            HackerNewsItem(
                external_id="story-1",
                title="Show HN: a trail planner",
                author="dang",
                published_at=_PUBLISHED_AT,
                engagement=140,
                url="https://news.ycombinator.com/item?id=1",
            )
        ],
        new_cursor=HackerNewsCursor(),
        quota_reached=False,
    )


class TestPerKindNormalization:
    def test_a_play_review_carries_its_rating_and_app_version(self) -> None:
        (item,) = items_from(SourceName.PLAY, play_payload())

        assert item.external_id == "play-1"
        assert item.author_handle == "Alice"
        assert item.body == "good app"
        assert item.published_at == _PUBLISHED_AT
        assert item.rating == 4
        assert item.app_version == "1.2.3"
        assert item.title is None
        assert item.url is None
        assert item.engagement is None

    def test_an_appstore_review_maps_onto_the_same_fields_as_play(self) -> None:
        (item,) = items_from(SourceName.APPSTORE, appstore_payload())

        assert item.external_id == "appstore-1"
        assert item.author_handle == "Bob"
        assert item.body == "crashes often"
        assert item.rating == 2
        assert item.app_version == "9.9"
        assert item.engagement is None

    def test_a_discourse_post_carries_its_like_count_as_engagement(self) -> None:
        (item,) = items_from(SourceName.DISCOURSE, discourse_payload())

        assert item.external_id == "post-1"
        assert item.author_handle == "Carol"
        assert item.body == "the sync keeps stalling"
        assert item.engagement == 7
        # A search hit has neither a title nor a link of its own.
        assert item.title is None
        assert item.url is None

    def test_a_news_article_carries_its_title_text_and_resolved_url(self) -> None:
        (item,) = items_from(SourceName.NEWS, news_payload())

        assert item.external_id == "article-1"
        assert item.title == "Hiking app raises prices"
        assert item.body == "the extracted article body"
        assert item.url == "https://publisher.test/article"
        assert item.engagement is None

    def test_a_feed_entry_carries_its_summary_as_the_body(self) -> None:
        (item,) = items_from(SourceName.RSS, feed_payload("https://outlet.test/a"))

        assert item.external_id == "entry-0"
        assert item.title == "Entry 0"
        assert item.body == "summary 0"
        assert item.url == "https://outlet.test/a"

    def test_a_hacker_news_story_has_no_body_of_its_own(self) -> None:
        (item,) = items_from(SourceName.HACKERNEWS, hackernews_payload())

        assert item.external_id == "story-1"
        assert item.author_handle == "dang"
        assert item.title == "Show HN: a trail planner"
        assert item.body is None
        assert item.engagement == 140


class TestAbsentFieldsStayAbsent:
    def test_a_kind_with_no_rating_or_app_version_leaves_both_absent(self) -> None:
        # Only the four non-review kinds are testable here: PlayReview.rating and
        # AppStoreReview.rating are typed int, and both adapters coerce a missing
        # score to 0 during their own parsing (`fields["score"] or 0`), so an
        # absent rating never reaches items_from for those two kinds.
        payloads = {
            SourceName.DISCOURSE: discourse_payload(),
            SourceName.NEWS: news_payload(),
            SourceName.RSS: feed_payload("https://outlet.test/a"),
            SourceName.HACKERNEWS: hackernews_payload(),
        }

        for source_name, payload in payloads.items():
            (item,) = items_from(source_name, payload)
            assert item.rating is None, source_name
            assert item.app_version is None, source_name


class TestOutletDerivedHandle:
    def test_a_news_handle_is_the_outlet_host_without_www_or_case(self) -> None:
        (item,) = items_from(
            SourceName.NEWS, news_payload("https://WWW.Publisher.TEST/story/1")
        )

        assert item.author_handle == "publisher.test"

    def test_a_feed_entry_without_a_link_falls_back_to_the_unknown_outlet(self) -> None:
        linked, unlinked = items_from(
            SourceName.RSS, feed_payload("https://www.Outlet.test/a", "")
        )

        assert linked.author_handle == "outlet.test"
        assert unlinked.author_handle == _UNKNOWN_OUTLET


class TestBodyFallback:
    def test_a_story_with_no_body_becomes_a_row_bodied_by_its_title(self) -> None:
        source = MonitorSource(
            monitor_id=uuid.uuid4(), source_name=SourceName.HACKERNEWS, instance_key="trail planner"
        )
        items = items_from(SourceName.HACKERNEWS, hackernews_payload())

        (row,) = ingest._document_rows(source, items)

        assert items[0].body is None
        assert row["body"] == "Show HN: a trail planner"
        assert row["entity_id"] == "trail planner"


class FakeRedis:
    """Serves the fetch client's breaker, cache and throttle as well as the run
    lock, which is the one caller using ``nx`` and ``eval``."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._store.get(key)

    async def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> Any:
        if nx and key in self._store:
            return False
        self._store[key] = value if isinstance(value, str) else str(value)
        return True

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._store.pop(key, None)

    async def incr(self, key: str) -> int:
        value = int(self._store.get(key, 0)) + 1
        self._store[key] = str(value)
        return value

    async def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        if self._store.get(key) == token:
            del self._store[key]
            return 1
        return 0


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


class RoutingTransport:
    """Answers by URL rather than in call order: a run makes no promise about the
    order it collects a monitor's sources in."""

    def __init__(self, responses: dict[str, TransportResponse]) -> None:
        self._responses = responses

    async def send(self, request: FetchRequest) -> TransportResponse:
        return self._responses[request.url]


def build_client(transport: RoutingTransport) -> FetchClient:
    redis = FakeRedis()
    clock = FrozenClock()
    return FetchClient(
        breaker=Breaker(
            redis, BreakerSettings(failure_threshold=3, cooldown_seconds=60.0), clock=clock
        ),
        cache=ResponseCache(redis, ttl_seconds=3600),
        throttle=Throttle(redis, clock=clock, random_source=FixedRandom()),
        plain_transport=transport,
        impersonating_transport=transport,
        host_budget=HostBudget(
            capacity=100.0,
            refill_rate_per_second=1000.0,
            jitter_low_seconds=0.0,
            jitter_high_seconds=0.0,
        ),
        max_transport_retries=0,
    )


class UnusedResolver:
    """A feed source resolves no redirects and extracts no articles, so a call to
    either collaborator would mean the wrong plan ran."""

    async def resolve(self, google_news_url: str) -> str:
        raise AssertionError("the rss plan must not resolve redirects")


class UnusedExtractor:
    async def extract(self, url: str) -> str:
        raise AssertionError("the rss plan must not extract articles")


class NullDispatcher:
    def dispatch_run(self, run_id: uuid.UUID) -> None:
        pass


_FEED_A = "https://outlet-a.test/feed.xml"
_FEED_B = "https://outlet-b.test/feed.xml"


def feed_response(*entry_ids: str) -> TransportResponse:
    entries = "".join(
        f"<item><guid>{entry_id}</guid><title>Item {entry_id}</title>"
        f"<link>https://outlet-a.test/{entry_id}</link>"
        f"<description>summary of {entry_id}</description>"
        "<pubDate>Wed, 19 Aug 2026 00:00:00 GMT</pubDate></item>"
        for entry_id in entry_ids
    )
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Outlet</title>'
        f"{entries}</channel></rss>"
    ).encode()
    return TransportResponse(200, "application/xml", body, "fixture/1")


def wrong_content_type_response() -> TransportResponse:
    """An HTML error page where the feed should be. The guardrail reports
    UnexpectedContentType and the adapter raises its own collection failure."""
    return TransportResponse(200, "text/html", b"<html>rate limited</html>", "fixture/1")


# The entry id whose write the selectively failing repository rejects. Two RSS
# sources on one monitor are indistinguishable in a document row apart from the
# ids of the entries they collected.
_FAILING_ENTRY = "write-fails"


def write_failure() -> OperationalError:
    """The shape a dropped connection reaches ``collect_source`` in: SQLAlchemy
    wraps the driver's exception, so what propagates out of ``session.execute`` is
    a ``SQLAlchemyError`` carrying the original as ``orig``."""
    return OperationalError(
        "INSERT INTO documents ...", {}, ConnectionResetError("connection lost")
    )


class FailingDocumentRepository:
    """Provokes requirement 8's write failure the only way collection can reach
    it. ``collect_source`` gates the cursor write-back on rows having persisted,
    and that gate is left untouched — an insert that raises is the honest way to
    make the write fail rather than patching the gate itself."""

    def __init__(self, session: Any) -> None:
        pass

    async def insert_ignoring_duplicates(self, rows: list[dict[str, Any]]) -> int:
        raise write_failure()


class SelectivelyFailingDocumentRepository:
    """Fails one source's write and lets the source beside it persist for real,
    which is the only way to observe requirement 6 for a write failure: a repository
    that failed every write would leave the run with nothing collected at all."""

    def __init__(self, session: Any) -> None:
        self._real = DocumentRepository(session)

    async def insert_ignoring_duplicates(self, rows: list[dict[str, Any]]) -> int:
        if any(row["external_id"] == _FAILING_ENTRY for row in rows):
            raise write_failure()
        return await self._real.insert_ignoring_duplicates(rows)


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


async def _make_monitor(session_factory: async_sessionmaker) -> uuid.UUID:
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Hiking app",
            target_type=TargetType.PRODUCT,
            seed={"identifiers": ["com.example.hiking"]},
            cron_expression="0 3 * * *",
            timezone="UTC",
            retention_days=90,
        )
    )
    return monitor.id


async def _attach_feed(
    session_factory: async_sessionmaker, monitor_id: uuid.UUID, feed_url: str
) -> uuid.UUID:
    source = await MonitorSourceService(session_factory).attach(
        monitor_id,
        MonitorSourceCreateRequest(
            source_name=SourceName.RSS, config={"feed_url": feed_url}, daily_quota=10
        ),
    )
    return source.id


async def _clear_config(session_factory: async_sessionmaker, source_id: uuid.UUID) -> None:
    """Leave the row holding a config no plan can be built from, the way a row
    written before its kind's validation existed reads (design.md, "Trust
    boundary")."""
    async with session_factory() as session:
        source = await session.get(MonitorSource, source_id)
        source.config = {}
        await session.commit()


async def _create_run(session_factory: async_sessionmaker, monitor_id: uuid.UUID) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(IngestionRun(id=run_id, monitor_id=monitor_id, run_date=date.today()))
        await session.commit()
    return run_id


async def _collect(
    session_factory: async_sessionmaker,
    transport: RoutingTransport,
    run_id: uuid.UUID,
    source_id: uuid.UUID,
) -> FetchStats:
    return await collect_source(
        session_factory=session_factory,
        fetch_client=build_client(transport),
        resolver=UnusedResolver(),
        extractor=UnusedExtractor(),
        run_id=run_id,
        source_id=source_id,
    )


async def _count_stats(
    session_factory: async_sessionmaker, run_id: uuid.UUID, source_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count())
            .select_from(FetchStats)
            .where(FetchStats.run_id == run_id, FetchStats.monitor_source_id == source_id)
        )
        return result.scalar_one()


async def _stats_for(
    session_factory: async_sessionmaker, run_id: uuid.UUID, source_id: uuid.UUID
) -> FetchStats:
    async with session_factory() as session:
        result = await session.execute(
            select(FetchStats).where(
                FetchStats.run_id == run_id, FetchStats.monitor_source_id == source_id
            )
        )
        return result.scalar_one()


async def _read_cursor(
    session_factory: async_sessionmaker, source_id: uuid.UUID
) -> tuple[str | None, datetime | None]:
    async with session_factory() as session:
        source = await session.get(MonitorSource, source_id)
        return source.last_external_id, source.last_published_at


async def _read_run_status(
    session_factory: async_sessionmaker, run_id: uuid.UUID
) -> RunStatus:
    async with session_factory() as session:
        return (await session.get(IngestionRun, run_id)).status


def _stub_downstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaces the enrichment pipeline ``dispatch_run`` appends on a non-failed
    status: it loads embedding models, and it has its own tests in
    test_dispatch_downstream_pipeline.py."""

    async def no_downstream(session_factory: Any, *, run_id: uuid.UUID, monitor_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(dispatch, "_run_downstream_pipeline", no_downstream)


class TestLedgerRow:
    def test_a_successful_collection_writes_exactly_one_statistics_row(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_feed(session_factory, monitor_id, _FEED_A))
        run_id = run(_create_run(session_factory, monitor_id))
        transport = RoutingTransport({_FEED_A: feed_response("e1", "e2")})

        stats = run(_collect(session_factory, transport, run_id, source_id))

        assert stats.kept == 2
        assert stats.error is None
        assert run(_count_stats(session_factory, run_id, source_id)) == 1

    def test_an_adapter_failure_writes_exactly_one_statistics_row(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_feed(session_factory, monitor_id, _FEED_A))
        run_id = run(_create_run(session_factory, monitor_id))
        transport = RoutingTransport({_FEED_A: wrong_content_type_response()})

        stats = run(_collect(session_factory, transport, run_id, source_id))

        assert stats.error is not None
        assert stats.validation_failed is True
        assert run(_count_stats(session_factory, run_id, source_id)) == 1

    def test_a_healthy_but_empty_result_writes_exactly_one_statistics_row(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_feed(session_factory, monitor_id, _FEED_A))
        run_id = run(_create_run(session_factory, monitor_id))
        transport = RoutingTransport({_FEED_A: feed_response()})

        stats = run(_collect(session_factory, transport, run_id, source_id))

        assert stats.kept == 0
        assert stats.error is None
        assert run(_count_stats(session_factory, run_id, source_id)) == 1

    def test_an_unbuildable_config_writes_its_row_naming_the_fault(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_feed(session_factory, monitor_id, _FEED_A))
        run(_clear_config(session_factory, source_id))
        run_id = run(_create_run(session_factory, monitor_id))

        # A plan that will not build fails before any request, so the transport
        # holds no response at all.
        stats = run(_collect(session_factory, RoutingTransport({}), run_id, source_id))

        assert stats.validation_failed is True
        assert "config.feed_url" in stats.error
        assert run(_count_stats(session_factory, run_id, source_id)) == 1


class TestCursorAdvance:
    def test_the_cursor_does_not_advance_when_the_document_write_failed(
        self, session_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_feed(session_factory, monitor_id, _FEED_A))
        run_id = run(_create_run(session_factory, monitor_id))
        transport = RoutingTransport({_FEED_A: feed_response("e1")})
        monkeypatch.setattr(ingest, "DocumentRepository", FailingDocumentRepository)

        stats = run(_collect(session_factory, transport, run_id, source_id))

        assert run(_read_cursor(session_factory, source_id)) == (None, None)
        # The write failing is a collection outcome, not an escape: the row it is
        # recorded on is what every later surface reads (requirement 5).
        assert run(_count_stats(session_factory, run_id, source_id)) == 1
        assert "connection lost" in stats.error
        # The items were fine and the database was not, so this is transport-class.
        assert stats.validation_failed is False
        assert stats.kept == 0
        assert stats.duplicates == 0


class TestPartialFailure:
    def test_one_failing_source_beside_a_collecting_one_reports_the_run_partial(
        self, session_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        collecting_id = run(_attach_feed(session_factory, monitor_id, _FEED_A))
        failing_id = run(_attach_feed(session_factory, monitor_id, _FEED_B))
        run_id = run(_create_run(session_factory, monitor_id))
        transport = RoutingTransport(
            {_FEED_A: feed_response("e1"), _FEED_B: wrong_content_type_response()}
        )
        _stub_downstream(monkeypatch)

        run(
            dispatch.dispatch_run(
                session_factory=session_factory,
                run_service=RunService(session_factory, RunLock(FakeRedis()), NullDispatcher()),
                fetch_client=build_client(transport),
                run_id=run_id,
                monitor_id=monitor_id,
                resolver=UnusedResolver(),
                extractor=UnusedExtractor(),
            )
        )

        assert run(_read_run_status(session_factory, run_id)) is RunStatus.PARTIAL
        assert run(_stats_for(session_factory, run_id, collecting_id)).kept == 1
        assert run(_stats_for(session_factory, run_id, failing_id)).error is not None

    def test_a_failed_document_write_fails_only_its_own_source(
        self, session_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        collecting_id = run(_attach_feed(session_factory, monitor_id, _FEED_A))
        failing_id = run(_attach_feed(session_factory, monitor_id, _FEED_B))
        run_id = run(_create_run(session_factory, monitor_id))
        transport = RoutingTransport(
            {_FEED_A: feed_response("e1"), _FEED_B: feed_response(_FAILING_ENTRY)}
        )
        monkeypatch.setattr(
            ingest, "DocumentRepository", SelectivelyFailingDocumentRepository
        )
        _stub_downstream(monkeypatch)

        run(
            dispatch.dispatch_run(
                session_factory=session_factory,
                run_service=RunService(session_factory, RunLock(FakeRedis()), NullDispatcher()),
                fetch_client=build_client(transport),
                run_id=run_id,
                monitor_id=monitor_id,
                resolver=UnusedResolver(),
                extractor=UnusedExtractor(),
            )
        )

        failing = run(_stats_for(session_factory, run_id, failing_id))

        assert run(_read_run_status(session_factory, run_id)) is RunStatus.PARTIAL
        assert run(_stats_for(session_factory, run_id, collecting_id)).kept == 1
        assert failing.error is not None
        assert failing.validation_failed is False

    def test_an_unbuildable_config_fails_only_its_own_source(
        self, session_factory: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        collecting_id = run(_attach_feed(session_factory, monitor_id, _FEED_A))
        broken_id = run(_attach_feed(session_factory, monitor_id, _FEED_B))
        run(_clear_config(session_factory, broken_id))
        run_id = run(_create_run(session_factory, monitor_id))
        transport = RoutingTransport({_FEED_A: feed_response("e1", "e2")})
        _stub_downstream(monkeypatch)

        run(
            dispatch.dispatch_run(
                session_factory=session_factory,
                run_service=RunService(session_factory, RunLock(FakeRedis()), NullDispatcher()),
                fetch_client=build_client(transport),
                run_id=run_id,
                monitor_id=monitor_id,
                resolver=UnusedResolver(),
                extractor=UnusedExtractor(),
            )
        )

        collecting = run(_stats_for(session_factory, run_id, collecting_id))
        broken = run(_stats_for(session_factory, run_id, broken_id))

        assert collecting.kept == 2
        assert collecting.error is None
        assert broken.validation_failed is True
        assert run(_read_run_status(session_factory, run_id)) is RunStatus.PARTIAL
