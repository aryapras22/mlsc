"""Google News search adapter: resolve redirect, then extract article text.

An unresolvable redirect or an unextractable article is per-item and
recoverable — the item is counted as filtered and collection continues
(design.md, "Failure strategy"). A malformed feed response fails the source.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from time import mktime
from typing import Any

import feedparser

from mlsc.core.fetch.contracts import ClientProfile, FetchExpectations, FetchRequest, FetchStatus
from mlsc.sources.base import SourceAdapter
from mlsc.sources.news.extract import ArticleExtractor, UnextractableArticle
from mlsc.sources.news.resolve import RedirectResolver, UnresolvableRedirect
from mlsc.sources.registry import register

_HOST_KEY = "news.google.com"


@dataclasses.dataclass(frozen=True)
class NewsCursor:
    last_published_at: datetime | None = None


@dataclasses.dataclass(frozen=True)
class NewsArticle:
    external_id: str
    resolved_url: str
    title: str
    text: str
    published_at: datetime


class NewsCollectionFailed(RuntimeError):
    def __init__(self, status: FetchStatus, payload: Any) -> None:
        super().__init__(f"News collection failed: {status.value}: {payload}")
        self.status = status
        self.payload = payload


@dataclasses.dataclass(frozen=True)
class CollectionResult:
    articles: list[NewsArticle]
    new_cursor: NewsCursor
    quota_reached: bool
    filtered: int


@register("news")
class NewsAdapter(SourceAdapter):
    def __init__(
        self,
        fetch_client,  # noqa: ANN001
        *,
        query: str,
        resolver: RedirectResolver,
        extractor: ArticleExtractor,
    ) -> None:
        super().__init__(fetch_client)
        self._query = query
        self._resolver = resolver
        self._extractor = extractor

    @property
    def expectations(self) -> FetchExpectations:
        # Confirmed against the live endpoint: application/xml once the client
        # follows Google's redirect, application/binary if it does not.
        return FetchExpectations(content_type="application/xml", body_format="raw")

    async def fetch(self, entity: str, cursor: NewsCursor, quota: int) -> CollectionResult:
        request = FetchRequest(
            url="https://news.google.com/rss/search",
            host_key=_HOST_KEY,
            client_profile=ClientProfile.PLAIN,
            query=(("q", self._query),),
        )
        outcome = await self._fetch_client.get(request, self.expectations)
        if outcome.status is not FetchStatus.OK:
            raise NewsCollectionFailed(outcome.status, outcome.payload)

        parsed = feedparser.parse(outcome.payload if isinstance(outcome.payload, str) else "")
        if parsed.bozo and not parsed.entries:
            raise NewsCollectionFailed(FetchStatus.VALIDATION_FAILED, "unparseable feed")

        articles: list[NewsArticle] = []
        filtered = 0
        quota_reached = False
        for entry in parsed.entries:
            published_at = _entry_published_at(entry)
            if cursor.last_published_at is not None and published_at <= cursor.last_published_at:
                break
            try:
                resolved_url = await self._resolver.resolve(entry.link)
                text = await self._extractor.extract(resolved_url)
            except (UnresolvableRedirect, UnextractableArticle):
                filtered += 1
                continue
            articles.append(
                NewsArticle(
                    external_id=entry.get("id", entry.link),
                    resolved_url=resolved_url,
                    title=entry.get("title", ""),
                    text=text,
                    published_at=published_at,
                )
            )
            if len(articles) >= quota:
                quota_reached = True
                break

        new_cursor = (
            NewsCursor(last_published_at=articles[0].published_at) if articles else cursor
        )
        return CollectionResult(articles, new_cursor, quota_reached, filtered)


def _entry_published_at(entry: Any) -> datetime:
    parsed_time = entry.get("published_parsed")
    if parsed_time is None:
        return datetime.min
    return datetime.fromtimestamp(mktime(parsed_time))
