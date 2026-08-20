"""Generic RSS/Atom feed adapter for an arbitrary configured URL."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from time import mktime
from typing import Any

import feedparser

from mlsc.core.fetch.contracts import ClientProfile, FetchExpectations, FetchRequest, FetchStatus
from mlsc.sources.base import SourceAdapter
from mlsc.sources.registry import register

LIBRARY_VERSION = "feedparser"


@dataclasses.dataclass(frozen=True)
class FeedCursor:
    last_published_at: datetime | None = None


@dataclasses.dataclass(frozen=True)
class FeedItem:
    external_id: str
    title: str
    content: str | None
    published_at: datetime
    url: str


class FeedCollectionFailed(RuntimeError):
    def __init__(self, status: FetchStatus, payload: Any) -> None:
        super().__init__(f"Feed collection failed: {status.value}: {payload}")
        self.status = status
        self.payload = payload


class MalformedFeed(RuntimeError):
    """The feed parsed but exposes no entries container."""


@dataclasses.dataclass(frozen=True)
class CollectionResult:
    items: list[FeedItem]
    new_cursor: FeedCursor
    quota_reached: bool


@register("rss")
class FeedAdapter(SourceAdapter):
    def __init__(self, fetch_client, *, feed_url: str) -> None:  # noqa: ANN001
        super().__init__(fetch_client)
        self._feed_url = feed_url
        self._host_key = feed_url.split("//", 1)[-1].split("/", 1)[0]

    @property
    def expectations(self) -> FetchExpectations:
        return FetchExpectations(content_type="application/xml", body_format="raw")

    async def fetch(self, entity: str, cursor: FeedCursor, quota: int) -> CollectionResult:
        request = FetchRequest(
            url=self._feed_url, host_key=self._host_key, client_profile=ClientProfile.PLAIN
        )
        outcome = await self._fetch_client.get(request, self.expectations)
        if outcome.status is not FetchStatus.OK:
            raise FeedCollectionFailed(outcome.status, outcome.payload)

        parsed = feedparser.parse(outcome.payload)
        if not hasattr(parsed, "entries"):
            raise MalformedFeed(self._feed_url)

        collected: list[FeedItem] = []
        quota_reached = False
        for entry in parsed.entries:
            published_at = _entry_published_at(entry)
            if cursor.last_published_at is not None and published_at <= cursor.last_published_at:
                break
            collected.append(
                FeedItem(
                    external_id=entry.get("id", entry.get("link", "")),
                    title=entry.get("title", ""),
                    content=entry.get("summary"),
                    published_at=published_at,
                    url=entry.get("link", ""),
                )
            )
            if len(collected) >= quota:
                quota_reached = True
                break

        new_cursor = (
            FeedCursor(last_published_at=collected[0].published_at) if collected else cursor
        )
        return CollectionResult(collected, new_cursor, quota_reached)


def _entry_published_at(entry: Any) -> datetime:
    parsed_time = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed_time is None:
        return datetime.min
    return datetime.fromtimestamp(mktime(parsed_time))
