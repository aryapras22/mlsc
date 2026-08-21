"""Apple App Store review adapter, via the public customer-reviews RSS-as-JSON feed."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

from mlsc.core.fetch.contracts import ClientProfile, FetchExpectations, FetchRequest, FetchStatus
from mlsc.sources.base import SourceAdapter
from mlsc.sources.registry import register

LIBRARY_VERSION = "itunes-rss/1"
_HOST_KEY = "itunes.apple.com"

_SEARCH_EXPECTATIONS = FetchExpectations(
    content_type="text/javascript",
    item_path=("results",),
    required_fields=("trackId", "trackName", "bundleId"),
    min_rows_when_healthy=0,  # a narrow theme query may legitimately match nothing
)


@dataclasses.dataclass(frozen=True)
class AppStoreSearchResult:
    """One candidate app matched by a discovery query."""

    app_id: str
    bundle_id: str
    title: str
    seller_name: str
    description: str


@dataclasses.dataclass(frozen=True)
class AppStoreCursor:
    last_external_id: str | None = None


@dataclasses.dataclass(frozen=True)
class AppStoreReview:
    external_id: str
    username: str
    content: str | None
    rating: int
    published_at: datetime
    app_version: str | None


class AppStoreCollectionFailed(RuntimeError):
    def __init__(self, status: FetchStatus, payload: Any) -> None:
        super().__init__(f"App Store collection failed: {status.value}: {payload}")
        self.status = status
        self.payload = payload


@dataclasses.dataclass(frozen=True)
class CollectionResult:
    reviews: list[AppStoreReview]
    new_cursor: AppStoreCursor
    quota_reached: bool


@register("appstore")
class AppStoreAdapter(SourceAdapter):
    """Walks numbered RSS pages newest-first, stopping at the cursor or the allowance."""

    @property
    def expectations(self) -> FetchExpectations:
        return FetchExpectations(
            content_type="text/javascript",
            item_path=("feed", "entry"),
            min_rows_when_healthy=1,
        )

    async def fetch(self, entity: str, cursor: AppStoreCursor, quota: int) -> CollectionResult:
        collected: list[AppStoreReview] = []
        page = 1
        while len(collected) < quota:
            request = _build_request(entity, page)
            outcome = await self._fetch_client.get(request, self.expectations)
            if outcome.status is not FetchStatus.OK:
                raise AppStoreCollectionFailed(outcome.status, outcome.payload)

            entries = outcome.payload
            if not entries:
                break

            stopped = False
            for entry in entries:
                review = _parse_review(entry)
                if cursor.last_external_id is not None and review.external_id == cursor.last_external_id:
                    stopped = True
                    break
                collected.append(review)
                if len(collected) >= quota:
                    return CollectionResult(collected, _newest_cursor(collected, cursor), True)
            if stopped:
                break
            page += 1
            if page > 10:  # Apple's customer-reviews feed does not page past this in practice
                break

        return CollectionResult(collected, _newest_cursor(collected, cursor), False)

    async def discover(self, query: str) -> list[AppStoreSearchResult]:
        """Search the App Store's public catalog for apps matching ``query``.

        Theme-monitors requirement 3: this is a search over the store
        catalog, not a review collection — a distinct request against a
        distinct endpoint from ``fetch``, sharing only the adapter's
        ``FetchClient``.
        """
        request = FetchRequest(
            url="https://itunes.apple.com/search",
            host_key=_HOST_KEY,
            client_profile=ClientProfile.PLAIN,
            query=(("term", query), ("country", "us"), ("entity", "software"), ("limit", "25")),
        )
        outcome = await self._fetch_client.get(request, _SEARCH_EXPECTATIONS)
        if outcome.status is not FetchStatus.OK:
            raise AppStoreCollectionFailed(outcome.status, outcome.payload)
        return [_parse_search_result(entry) for entry in outcome.payload]


def _parse_search_result(entry: dict[str, Any]) -> AppStoreSearchResult:
    return AppStoreSearchResult(
        app_id=str(entry["trackId"]),
        bundle_id=entry["bundleId"],
        title=entry["trackName"],
        seller_name=entry.get("sellerName", ""),
        description=entry.get("description", ""),
    )


def _build_request(app_id: str, page: int) -> FetchRequest:
    url = (
        f"https://itunes.apple.com/us/rss/customerreviews/id={app_id}"
        f"/sortby=mostrecent/page={page}/json"
    )
    return FetchRequest(url=url, host_key=_HOST_KEY, client_profile=ClientProfile.PLAIN)


def _parse_review(entry: dict[str, Any]) -> AppStoreReview:
    def label(key: str) -> str | None:
        value = entry.get(key)
        return value.get("label") if isinstance(value, dict) else None

    published_raw = label("updated")
    published_at = datetime.fromisoformat(published_raw) if published_raw else datetime.min
    rating_raw = label("im:rating")
    return AppStoreReview(
        external_id=label("id") or "",
        username=(entry.get("author") or {}).get("name", {}).get("label", ""),
        content=label("content"),
        rating=int(rating_raw) if rating_raw else 0,
        published_at=published_at,
        app_version=label("im:version"),
    )


def _newest_cursor(reviews: list[AppStoreReview], fallback: AppStoreCursor) -> AppStoreCursor:
    if not reviews:
        return fallback
    return AppStoreCursor(last_external_id=reviews[0].external_id)
