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

# A bare `q` param makes Google 302 to a locale-qualified URL instead of
# serving the feed directly; supplying the locale up front avoids that
# redirect (the guarded transport doesn't follow redirects) entirely.
_LOCALE_PARAMS = (("hl", "en-US"), ("gl", "US"), ("ceid", "US:en"))


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


@dataclasses.dataclass(frozen=True)
class NewsQueryViability:
    """Whether a generated query surfaces real news coverage, and how much.

    Requirement 3's "news query" discovery surface: a theme has no fixed
    news entity to search for, so what is discovered here is the query's
    own viability as a NEWS source — its ``entity_ref`` is the query text
    itself, matching how ``_validate_news_config`` already keys a NEWS
    source by its configured queries.
    """

    matched_titles: list[str]


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
        # Confirmed against the live endpoint: including the locale params
        # in the request avoids Google's redirect, so application/xml
        # comes back on the first request.
        return FetchExpectations(content_type="application/xml", body_format="raw")

    async def fetch(self, entity: str, cursor: NewsCursor, quota: int) -> CollectionResult:
        request = FetchRequest(
            url="https://news.google.com/rss/search",
            host_key=_HOST_KEY,
            client_profile=ClientProfile.PLAIN,
            query=(("q", self._query), *_LOCALE_PARAMS),
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

    async def discover(self, query: str) -> list[NewsQueryViability]:
        """Check whether ``query`` surfaces real news coverage.

        A search over titles only, without resolving redirects or
        extracting article text — discovery needs to know whether the
        query is worth attaching as a source, not to collect its content
        yet (theme-monitors design.md, "Success path": discovery maps
        results to candidates with their reason).
        """
        request = FetchRequest(
            url="https://news.google.com/rss/search",
            host_key=_HOST_KEY,
            client_profile=ClientProfile.PLAIN,
            query=(("q", query), *_LOCALE_PARAMS),
        )
        outcome = await self._fetch_client.get(request, self.expectations)
        if outcome.status is not FetchStatus.OK:
            raise NewsCollectionFailed(outcome.status, outcome.payload)

        parsed = feedparser.parse(outcome.payload if isinstance(outcome.payload, str) else "")
        if not parsed.entries:
            return []
        titles = [entry.get("title", "") for entry in parsed.entries[:5]]
        return [NewsQueryViability(matched_titles=titles)]


def _entry_published_at(entry: Any) -> datetime:
    parsed_time = entry.get("published_parsed")
    if parsed_time is None:
        return datetime.min
    return datetime.fromtimestamp(mktime(parsed_time))
