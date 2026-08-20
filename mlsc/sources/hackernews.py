"""Hacker News adapter via the Algolia search API. Timestamp cursor, date-windowed."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

from mlsc.core.fetch.contracts import ClientProfile, FetchExpectations, FetchRequest, FetchStatus
from mlsc.sources.base import SourceAdapter
from mlsc.sources.registry import register

LIBRARY_VERSION = "hn-algolia/1"
_HOST_KEY = "hn.algolia.com"


@dataclasses.dataclass(frozen=True)
class HackerNewsCursor:
    last_published_at: datetime | None = None


@dataclasses.dataclass(frozen=True)
class HackerNewsItem:
    external_id: str
    title: str
    author: str
    published_at: datetime
    engagement: int | None
    url: str | None


class HackerNewsCollectionFailed(RuntimeError):
    def __init__(self, status: FetchStatus, payload: Any) -> None:
        super().__init__(f"Hacker News collection failed: {status.value}: {payload}")
        self.status = status
        self.payload = payload


@dataclasses.dataclass(frozen=True)
class CollectionResult:
    items: list[HackerNewsItem]
    new_cursor: HackerNewsCursor
    quota_reached: bool


@register("hackernews")
class HackerNewsAdapter(SourceAdapter):
    def __init__(self, fetch_client, *, query: str) -> None:  # noqa: ANN001
        super().__init__(fetch_client)
        self._query = query

    @property
    def expectations(self) -> FetchExpectations:
        return FetchExpectations(
            content_type="application/json",
            item_path=("hits",),
            required_fields=("objectID", "created_at"),
            min_rows_when_healthy=0,
        )

    async def fetch(self, entity: str, cursor: HackerNewsCursor, quota: int) -> CollectionResult:
        request = FetchRequest(
            url="https://hn.algolia.com/api/v1/search_by_date",
            host_key=_HOST_KEY,
            client_profile=ClientProfile.PLAIN,
            query=(("query", self._query), ("tags", "story")),
        )
        outcome = await self._fetch_client.get(request, self.expectations)
        if outcome.status is not FetchStatus.OK:
            raise HackerNewsCollectionFailed(outcome.status, outcome.payload)

        collected: list[HackerNewsItem] = []
        quota_reached = False
        for hit in outcome.payload:
            item = _parse_hit(hit)
            if cursor.last_published_at is not None and item.published_at <= cursor.last_published_at:
                break
            collected.append(item)
            if len(collected) >= quota:
                quota_reached = True
                break

        new_cursor = (
            HackerNewsCursor(last_published_at=collected[0].published_at)
            if collected
            else cursor
        )
        return CollectionResult(collected, new_cursor, quota_reached)


def _parse_hit(hit: dict[str, Any]) -> HackerNewsItem:
    return HackerNewsItem(
        external_id=hit["objectID"],
        title=hit.get("title", ""),
        author=hit.get("author", ""),
        published_at=datetime.fromisoformat(hit["created_at"].replace("Z", "+00:00")),
        engagement=hit.get("points"),
        url=hit.get("url"),
    )
