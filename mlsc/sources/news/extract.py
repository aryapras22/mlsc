"""Extracts article text from a resolved publisher URL, via trafilatura."""

from __future__ import annotations

from importlib.metadata import version
from typing import Protocol

import trafilatura

from mlsc.core.fetch.client import FetchClient
from mlsc.core.fetch.contracts import ClientProfile, FetchExpectations, FetchRequest, FetchStatus

LIBRARY_VERSION = version("trafilatura")

# Publisher pages don't agree on one content type any more than RSS feeds did:
# most serve text/html, a minority still serve application/xhtml+xml. Neither
# is gated tighter than that, because trafilatura.extract() below is the real
# arbiter of whether the page was usable — an empty result already becomes
# UnextractableArticle, so a stricter content-type check would only add a
# second name for the same outcome.
_EXPECTATIONS = FetchExpectations(
    content_type=("text/html", "application/xhtml+xml"), body_format="raw"
)


class UnextractableArticle(RuntimeError):
    """The publisher page yielded no usable text."""


class ArticleExtractor(Protocol):
    async def extract(self, url: str) -> str: ...


class TrafilaturaExtractor:
    def __init__(self, fetch_client: FetchClient) -> None:
        self._fetch_client = fetch_client

    async def extract(self, url: str) -> str:
        # Unlike FeedAdapter/DiscourseAdapter, this class is not configured
        # with one host — each call is a different publisher, so host_key is
        # derived from the URL at call time rather than at construction.
        host_key = url.split("//", 1)[-1].split("/", 1)[0]
        request = FetchRequest(url=url, host_key=host_key, client_profile=ClientProfile.PLAIN)
        outcome = await self._fetch_client.get(request, _EXPECTATIONS)
        # A validation failure, a transport failure, and a breaker-open skip
        # are all "this article was not usable" to NewsAdapter (which already
        # catches UnextractableArticle and counts it as filtered) — the same
        # collapse NewsCollectionFailed's callers already accept elsewhere.
        if outcome.status is not FetchStatus.OK:
            raise UnextractableArticle(url)
        text = trafilatura.extract(outcome.payload)
        if not text:
            raise UnextractableArticle(url)
        return text
