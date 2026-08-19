"""Google Play review adapter.

Reuses ``google-play-scraper``'s request-building and response-parsing
knowledge, but never calls its ``post()``: every request goes through
``FetchClient`` instead, so Play reviews get the same throttling, circuit
breaking, caching, and validation as every other source (requirement 1).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from importlib.metadata import version
from typing import Any

from google_play_scraper.constants.element import ElementSpecs
from google_play_scraper.constants.google_play import Sort
from google_play_scraper.constants.request import Formats

from mlsc.core.fetch.contracts import (
    ClientProfile,
    FetchExpectations,
    FetchRequest,
    FetchStatus,
)
from mlsc.sources.base import SourceAdapter
from mlsc.sources.registry import register

LIBRARY_VERSION = version("google-play-scraper")

_HOST_KEY = "play.google.com"
_PAGE_SIZE = 100


@dataclasses.dataclass(frozen=True)
class PlayCursor:
    """The watermark: the newest review this source has already persisted."""

    last_external_id: str | None = None
    last_published_at: datetime | None = None


@dataclasses.dataclass(frozen=True)
class PlayReview:
    """One parsed review, before author hashing or content hashing."""

    external_id: str
    username: str
    content: str | None
    rating: int
    published_at: datetime
    app_version: str | None


class PlayCollectionFailed(RuntimeError):
    """Wraps a validation or transport failure the shared client reported."""

    def __init__(self, status: FetchStatus, payload: Any) -> None:
        super().__init__(f"Play collection failed: {status.value}: {payload}")
        self.status = status
        self.payload = payload


@dataclasses.dataclass(frozen=True)
class CollectionResult:
    """What one call to ``PlayAdapter.collect`` produced.

    ``quota_reached`` is ``True`` only when collection stopped because the
    allowance ran out, not because the cursor or Play itself ran out of new
    items — that distinction is what tells the caller whether ``kept`` is a
    measurement or a floor (design.md, "Domain shapes").
    """

    reviews: list[PlayReview]
    new_cursor: PlayCursor
    quota_reached: bool


@register("play")
class PlayAdapter(SourceAdapter):
    """Fetches new-to-old, stopping at the stored cursor or the daily allowance."""

    @property
    def expectations(self) -> FetchExpectations:
        # item_path is empty deliberately: the continuation token lives
        # alongside the review list inside the same decoded structure, so the
        # adapter gets the whole structure back and walks to the review list
        # itself. count_path still points the emptiness floor at that list.
        return FetchExpectations(
            content_type="application/json",
            item_path=(),
            count_path=(0,),
            min_rows_when_healthy=1,
            body_format="google_batchexecute",
        )

    async def fetch(self, entity: str, cursor: PlayCursor, quota: int) -> CollectionResult:
        """Collect reviews newest-first for the Play package ``entity``.

        Stops at the daily allowance, at the stored cursor, or when Play has no
        further pages. Raises ``PlayCollectionFailed`` on the first validation
        or transport failure — the caller decides whether rows already
        collected are kept (design.md, "Failure strategy": partial page
        success falls back to keeping what validated).
        """
        collected: list[PlayReview] = []
        continuation_token: str | None = None
        quota_reached = False

        while len(collected) < quota:
            page_size = min(_PAGE_SIZE, quota - len(collected))
            request = _build_request(entity, page_size, continuation_token)
            outcome = await self._fetch_client.get(request, self.expectations)

            if outcome.status is not FetchStatus.OK:
                raise PlayCollectionFailed(outcome.status, outcome.payload)

            envelope = outcome.payload
            raw_items: list[Any] = envelope[0] if envelope else []
            if not raw_items:
                break

            stopped_at_cursor = False
            for raw_item in raw_items:
                review = _parse_review(raw_item)
                if _at_or_past_cursor(review, cursor):
                    stopped_at_cursor = True
                    break
                collected.append(review)
                if len(collected) >= quota:
                    quota_reached = True
                    break
            if stopped_at_cursor or quota_reached:
                break

            continuation_token = _extract_continuation_token(envelope)
            if continuation_token is None:
                break

        new_cursor = _newest_cursor(collected, fallback=cursor)
        return CollectionResult(reviews=collected, new_cursor=new_cursor, quota_reached=quota_reached)


def _build_request(package_id: str, count: int, continuation_token: str | None) -> FetchRequest:
    url = Formats.Reviews.build(lang="en", country="us")
    body = Formats.Reviews.build_body(
        package_id,
        Sort.NEWEST.value,
        count,
        "null",
        "null",
        continuation_token,
    )
    return FetchRequest(
        url=url,
        host_key=_HOST_KEY,
        client_profile=ClientProfile.PLAIN,
        method="POST",
        headers=(("content-type", "application/x-www-form-urlencoded"),),
        body=body,
    )


def _parse_review(raw_item: Any) -> PlayReview:
    fields = {name: spec.extract_content(raw_item) for name, spec in ElementSpecs.Review.items()}
    published_at = fields["at"] or datetime.min
    return PlayReview(
        external_id=fields["reviewId"],
        username=fields["userName"] or "",
        content=fields["content"],
        rating=fields["score"] or 0,
        published_at=published_at,
        app_version=fields["appVersion"],
    )


def _at_or_past_cursor(review: PlayReview, cursor: PlayCursor) -> bool:
    if cursor.last_external_id is not None and review.external_id == cursor.last_external_id:
        return True
    if cursor.last_published_at is not None and review.published_at <= cursor.last_published_at:
        return True
    return False


def _extract_continuation_token(batchexecute_payload: Any) -> str | None:
    """Google's continuation token lives at a fixed offset in the envelope's second element."""
    try:
        return batchexecute_payload[-2][-1]
    except (IndexError, TypeError):
        return None


def _newest_cursor(reviews: list[PlayReview], *, fallback: PlayCursor) -> PlayCursor:
    """The cursor advances only to the newest row actually collected, never further.

    Reviews arrive newest-first, so the first collected row is the newest one;
    with nothing new collected the cursor is unchanged (learn.md, "Cursor /
    watermark incremental collection").
    """
    if not reviews:
        return fallback
    newest = reviews[0]
    return PlayCursor(last_external_id=newest.external_id, last_published_at=newest.published_at)
