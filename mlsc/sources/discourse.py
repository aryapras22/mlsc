"""Discourse forum adapter, driven entirely by a configured base URL.

Any Discourse instance works without a code change: the base URL is a
configuration value, not a constant (requirement 2).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

from mlsc.core.fetch.contracts import ClientProfile, FetchExpectations, FetchRequest, FetchStatus
from mlsc.sources.base import SourceAdapter
from mlsc.sources.registry import register

LIBRARY_VERSION = "discourse-search-json/1"


@dataclasses.dataclass(frozen=True)
class DiscourseCursor:
    last_published_at: datetime | None = None


@dataclasses.dataclass(frozen=True)
class DiscoursePost:
    external_id: str
    username: str
    content: str | None
    published_at: datetime
    engagement: int | None


class DiscourseCollectionFailed(RuntimeError):
    def __init__(self, status: FetchStatus, payload: Any) -> None:
        super().__init__(f"Discourse collection failed: {status.value}: {payload}")
        self.status = status
        self.payload = payload


@dataclasses.dataclass(frozen=True)
class CollectionResult:
    posts: list[DiscoursePost]
    new_cursor: DiscourseCursor
    quota_reached: bool


@register("discourse")
class DiscourseAdapter(SourceAdapter):
    """Searches ``/search.json`` for a query, walking posts newest-first."""

    def __init__(self, fetch_client, *, base_url: str, query: str) -> None:  # noqa: ANN001
        super().__init__(fetch_client)
        self._base_url = base_url
        self._host_key = base_url.split("//", 1)[-1].split("/", 1)[0]
        self._query = query

    @property
    def expectations(self) -> FetchExpectations:
        return FetchExpectations(
            content_type="application/json",
            item_path=("posts",),
            required_fields=("id", "created_at"),
            min_rows_when_healthy=0,  # a search query may legitimately return nothing
        )

    async def fetch(self, entity: str, cursor: DiscourseCursor, quota: int) -> CollectionResult:
        request = FetchRequest(
            url=f"{self._base_url}/search.json",
            host_key=self._host_key,
            client_profile=ClientProfile.PLAIN,
            query=(("q", self._query),),
        )
        outcome = await self._fetch_client.get(request, self.expectations)
        if outcome.status is not FetchStatus.OK:
            raise DiscourseCollectionFailed(outcome.status, outcome.payload)

        collected: list[DiscoursePost] = []
        quota_reached = False
        for raw_post in outcome.payload:
            post = _parse_post(raw_post)
            if cursor.last_published_at is not None and post.published_at <= cursor.last_published_at:
                break
            collected.append(post)
            if len(collected) >= quota:
                quota_reached = True
                break

        new_cursor = (
            DiscourseCursor(last_published_at=collected[0].published_at)
            if collected
            else cursor
        )
        return CollectionResult(collected, new_cursor, quota_reached)


def _parse_post(raw: dict[str, Any]) -> DiscoursePost:
    return DiscoursePost(
        external_id=str(raw["id"]),
        username=raw.get("username", ""),
        content=raw.get("blurb") or raw.get("cooked"),
        published_at=datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00")),
        engagement=raw.get("like_count"),
    )
