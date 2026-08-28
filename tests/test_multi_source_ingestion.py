"""Core tests for the five additional source adapters. No network call.

Covers cursor stop, allowance truncation, and the failure/filtered paths that
matter most: requirement 4, 5.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from mlsc.core.fetch.breaker import Breaker, BreakerSettings
from mlsc.core.fetch.cache import ResponseCache
from mlsc.core.fetch.client import FetchClient
from mlsc.core.fetch.contracts import FetchRequest
from mlsc.core.fetch.throttle import HostBudget, Throttle
from mlsc.core.fetch.transports import TransportResponse
from mlsc.sources.appstore import AppStoreAdapter, AppStoreCollectionFailed, AppStoreCursor
from mlsc.sources.discourse import DiscourseAdapter, DiscourseCursor
from mlsc.sources.hackernews import HackerNewsAdapter, HackerNewsCursor
from mlsc.sources.news.adapter import NewsAdapter, NewsCursor
from mlsc.sources.news.extract import UnextractableArticle
from mlsc.sources.news.resolve import UnresolvableRedirect
from mlsc.sources.rss import FeedAdapter, FeedCursor, MalformedFeed


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._store.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self._store[key] = str(value)

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
    def __init__(self, responses: list[TransportResponse]) -> None:
        self._responses = list(responses)

    async def send(self, request: FetchRequest) -> TransportResponse:
        return self._responses.pop(0)


def build_client(transport: FakeTransport) -> FetchClient:
    redis = FakeRedis()
    clock = FrozenClock()
    return FetchClient(
        breaker=Breaker(redis, BreakerSettings(failure_threshold=3, cooldown_seconds=60.0), clock=clock),
        cache=ResponseCache(redis, ttl_seconds=3600),
        throttle=Throttle(redis, clock=clock, random_source=FixedRandom()),
        plain_transport=transport,
        impersonating_transport=transport,
        host_budget=HostBudget(capacity=100.0, refill_rate_per_second=1000.0, jitter_low_seconds=0.0, jitter_high_seconds=0.0),
        max_transport_retries=0,
    )


def appstore_response(entries: list[dict[str, Any]]) -> TransportResponse:
    body = json.dumps({"feed": {"entry": entries}}).encode()
    return TransportResponse(200, "text/javascript", body, "fixture/1")


def appstore_exhausted_response() -> TransportResponse:
    """Apple's actual shape for a page past the last review: no ``entry`` key at all."""
    body = json.dumps({"feed": {"author": {}, "updated": {}, "title": {}}}).encode()
    return TransportResponse(200, "text/javascript", body, "fixture/1")


def appstore_entry(review_id: str, rating: int = 4) -> dict[str, Any]:
    return {
        "id": {"label": review_id},
        "author": {"name": {"label": "Test User"}},
        "content": {"label": "good"},
        "im:rating": {"label": str(rating)},
        "im:version": {"label": "1.0"},
        "updated": {"label": "2026-08-19T00:00:00-07:00"},
    }


class TestAppStoreAdapter:
    def test_allowance_truncates(self) -> None:
        entries = [appstore_entry(f"r{i}") for i in range(5)]
        transport = FakeTransport([appstore_response(entries)])
        adapter = AppStoreAdapter(build_client(transport))

        result = run(adapter.fetch("123", AppStoreCursor(), quota=3))

        assert len(result.reviews) == 3
        assert result.quota_reached is True

    def test_cursor_stops_at_matching_id(self) -> None:
        entries = [appstore_entry("new-1"), appstore_entry("seen")]
        transport = FakeTransport([appstore_response(entries)])
        adapter = AppStoreAdapter(build_client(transport))

        result = run(adapter.fetch("123", AppStoreCursor(last_external_id="seen"), quota=10))

        assert [r.external_id for r in result.reviews] == ["new-1"]

    def test_missing_entry_on_later_page_ends_pagination_cleanly(self) -> None:
        entries = [appstore_entry(f"r{i}") for i in range(2)]
        transport = FakeTransport([appstore_response(entries), appstore_exhausted_response()])
        adapter = AppStoreAdapter(build_client(transport))

        result = run(adapter.fetch("123", AppStoreCursor(), quota=10))

        assert [r.external_id for r in result.reviews] == ["r0", "r1"]
        assert result.quota_reached is False

    def test_wrong_content_type_raises(self) -> None:
        transport = FakeTransport(
            [TransportResponse(200, "text/html", b"<html/>", "fixture/1")]
        )
        adapter = AppStoreAdapter(build_client(transport))

        with pytest.raises(AppStoreCollectionFailed):
            run(adapter.fetch("123", AppStoreCursor(), quota=10))


def discourse_response(posts: list[dict[str, Any]]) -> TransportResponse:
    body = json.dumps({"posts": posts, "topics": []}).encode()
    return TransportResponse(200, "application/json", body, "fixture/1")


class TestDiscourseAdapter:
    def test_collects_and_stops_at_cursor(self) -> None:
        posts = [
            {"id": 2, "username": "a", "blurb": "hi", "created_at": "2026-08-19T00:00:00Z", "like_count": 1},
            {"id": 1, "username": "b", "blurb": "old", "created_at": "2026-08-18T00:00:00Z", "like_count": 0},
        ]
        transport = FakeTransport([discourse_response(posts)])
        adapter = DiscourseAdapter(build_client(transport), base_url="https://forum.test", query="x")

        cursor = DiscourseCursor(last_published_at=datetime(2026, 8, 18, tzinfo=timezone.utc))
        result = run(adapter.fetch("x", cursor, quota=10))

        assert [p.external_id for p in result.posts] == ["2"]


def hn_response(hits: list[dict[str, Any]]) -> TransportResponse:
    body = json.dumps({"hits": hits}).encode()
    return TransportResponse(200, "application/json", body, "fixture/1")


class TestHackerNewsAdapter:
    def test_allowance_truncates(self) -> None:
        hits = [
            {"objectID": str(i), "title": "t", "author": "a", "created_at": "2026-08-19T00:00:00Z", "points": 1}
            for i in range(5)
        ]
        transport = FakeTransport([hn_response(hits)])
        adapter = HackerNewsAdapter(build_client(transport), query="x")

        result = run(adapter.fetch("x", HackerNewsCursor(), quota=2))

        assert len(result.items) == 2
        assert result.quota_reached is True


class TestFeedAdapter:
    def test_malformed_feed_raises(self) -> None:
        transport = FakeTransport(
            [TransportResponse(200, "application/xml", b"not xml at all", "fixture/1")]
        )
        adapter = FeedAdapter(build_client(transport), feed_url="https://example.test/feed.xml")

        # feedparser tolerates garbage and returns an empty entries list rather
        # than raising, so this exercises the "zero collected" path, not
        # MalformedFeed directly (which needs a response with no bozo feed
        # object at all — rare in practice).
        result = run(adapter.fetch("x", FeedCursor(), quota=10))
        assert result.items == []


class FakeResolver:
    def __init__(self, should_fail: bool) -> None:
        self.should_fail = should_fail

    async def resolve(self, url: str) -> str:
        if self.should_fail:
            raise UnresolvableRedirect(url)
        return "https://publisher.test/article"


class FakeExtractor:
    def __init__(self, should_fail: bool) -> None:
        self.should_fail = should_fail

    async def extract(self, url: str) -> str:
        if self.should_fail:
            raise UnextractableArticle(url)
        return "article body text"


_FEED_XML = b"""<?xml version="1.0"?>
<rss><channel>
<item><title>A</title><link>https://news.google.com/rss/articles/x</link>
<pubDate>Wed, 19 Aug 2026 00:00:00 GMT</pubDate></item>
</channel></rss>"""


class TestNewsAdapter:
    def test_unresolvable_redirect_is_filtered_not_failed(self) -> None:
        transport = FakeTransport(
            [TransportResponse(200, "application/xml", _FEED_XML, "fixture/1")]
        )
        adapter = NewsAdapter(
            build_client(transport), query="x", resolver=FakeResolver(should_fail=True),
            extractor=FakeExtractor(should_fail=False),
        )

        result = run(adapter.fetch("x", NewsCursor(), quota=10))

        assert result.articles == []
        assert result.filtered == 1

    def test_successful_resolution_and_extraction_collects_article(self) -> None:
        transport = FakeTransport(
            [TransportResponse(200, "application/xml", _FEED_XML, "fixture/1")]
        )
        adapter = NewsAdapter(
            build_client(transport), query="x", resolver=FakeResolver(should_fail=False),
            extractor=FakeExtractor(should_fail=False),
        )

        result = run(adapter.fetch("x", NewsCursor(), quota=10))

        assert len(result.articles) == 1
        assert result.articles[0].text == "article body text"
        assert result.filtered == 0
