"""Kind-agnostic collection vocabulary: one item shape and one failure type.

Only this module and the adapters beside it know about source kinds; everything
downstream of it is uniform, which is what makes collecting from six kinds one
code path rather than six (design.md, "Success path").
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from mlsc.core.fetch.client import FetchClient
from mlsc.db.models import MonitorSource, SourceName
from mlsc.sources.appstore import LIBRARY_VERSION as APPSTORE_LIBRARY_VERSION
from mlsc.sources.appstore import CollectionResult as AppStoreResult
from mlsc.sources.appstore import AppStoreAdapter, AppStoreCursor
from mlsc.sources.base import SourceAdapter
from mlsc.sources.discourse import LIBRARY_VERSION as DISCOURSE_LIBRARY_VERSION
from mlsc.sources.discourse import CollectionResult as DiscourseResult
from mlsc.sources.discourse import DiscourseAdapter, DiscourseCursor
from mlsc.sources.hackernews import LIBRARY_VERSION as HACKERNEWS_LIBRARY_VERSION
from mlsc.sources.hackernews import CollectionResult as HackerNewsResult
from mlsc.sources.hackernews import HackerNewsAdapter, HackerNewsCursor
from mlsc.sources.news.adapter import CollectionResult as NewsResult
from mlsc.sources.news.adapter import NewsAdapter, NewsCursor

# The news adapter module declares no version of its own: what it returns is
# trafilatura's extracted text, so trafilatura is the library whose upgrade can
# change the payload, and therefore the one worth recording in the ledger.
from mlsc.sources.news.extract import LIBRARY_VERSION as NEWS_LIBRARY_VERSION
from mlsc.sources.news.extract import ArticleExtractor
from mlsc.sources.news.resolve import RedirectResolver
from mlsc.sources.play import LIBRARY_VERSION as PLAY_LIBRARY_VERSION
from mlsc.sources.play import CollectionResult as PlayResult
from mlsc.sources.play import PlayAdapter, PlayCursor
from mlsc.sources.rss import LIBRARY_VERSION as RSS_LIBRARY_VERSION
from mlsc.sources.rss import CollectionResult as FeedResult
from mlsc.sources.rss import FeedAdapter, FeedCursor


@dataclasses.dataclass(frozen=True)
class CollectedItem:
    """One item from any source, after its adapter's payload is normalized and
    before it becomes a document row.

    Every optional field is genuinely absent for some kind — a Hacker News
    story has no rating, a Play review no engagement count — so each is
    nullable rather than defaulted: an absent rating and a rating of zero are
    different facts (requirement 4, C5).
    """

    external_id: str
    author_handle: str | None
    title: str | None
    body: str | None
    published_at: datetime
    url: str | None
    rating: int | None
    app_version: str | None
    engagement: int | None


class SourceCollectionFailed(RuntimeError):
    """The single failure type collection raises, whatever the kind.

    Each adapter raises its own ``*CollectionFailed``; translating them into
    this one stops ``collect_one_source`` importing every adapter's exception
    module to decide what happened (design-method, "each layer translates
    failures into its own vocabulary").

    ``validation_failed`` separates a payload-shape violation from a transport
    fault, the distinction the caller turns into ``FAILED_VALIDATION`` rather
    than ``FAILED_TRANSPORT``.
    """

    def __init__(self, source_name: SourceName, message: str, *, validation_failed: bool) -> None:
        super().__init__(f"{source_name.value} collection failed: {message}")
        self.source_name = source_name
        self.message = message
        self.validation_failed = validation_failed


@dataclasses.dataclass(frozen=True)
class SourcePlan:
    """What one enabled source needs in order to run.

    ``adapters`` is a list rather than a single adapter because a news or Hacker
    News source carries several queries and each query is bound into its own
    adapter instance — the whole fan-out then runs under the source's one daily
    allowance and writes one ledger row (design.md, "Domain shapes").

    ``entity`` is what Play and App Store address their request with. The other
    four bind their query or feed URL into the adapter instance and their
    ``fetch`` never reads ``entity``, so the plan passes the source's
    ``instance_key`` — the same value the document row records as its
    ``entity_id`` (design.md, "``entity_id`` is the source's ``instance_key``").
    """

    adapters: list[SourceAdapter]
    entity: str
    cursor: Any
    library_version: str


def plan_for(
    source: MonitorSource,
    fetch_client: FetchClient,
    resolver: RedirectResolver,
    extractor: ArticleExtractor,
) -> SourcePlan:
    """Build the plan for one stored source row, whatever its kind.

    A ``SourceName`` with no builder here is an invariant violation, not a
    domain outcome, so the lookup is left to raise: dressing a missing plan as a
    routine skip is the defect this spec exists to fix (design.md, "Failure
    strategy").
    """
    return _PLAN_BUILDERS[source.source_name](source, fetch_client, resolver, extractor)


def _required_str(source: MonitorSource, field: str) -> str:
    """Read a config field attachment already validated, failing by name if the
    row predates that validation (requirement 7, design.md "Trust boundary")."""
    value = source.config.get(field)
    if not isinstance(value, str) or not value:
        raise SourceCollectionFailed(
            source.source_name,
            f"config.{field} is missing or not a non-empty string",
            validation_failed=True,
        )
    return value


def _required_queries(source: MonitorSource) -> list[str]:
    queries = source.config.get("queries")
    if (
        not isinstance(queries, list)
        or not queries
        or not all(isinstance(query, str) and query for query in queries)
    ):
        raise SourceCollectionFailed(
            source.source_name,
            "config.queries is missing or not a non-empty list of strings",
            validation_failed=True,
        )
    return queries


def _play_plan(
    source: MonitorSource,
    fetch_client: FetchClient,
    resolver: RedirectResolver,
    extractor: ArticleExtractor,
) -> SourcePlan:
    return SourcePlan(
        adapters=[PlayAdapter(fetch_client)],
        entity=_required_str(source, "package_id"),
        cursor=PlayCursor(
            last_external_id=source.last_external_id,
            last_published_at=source.last_published_at,
        ),
        library_version=PLAY_LIBRARY_VERSION,
    )


def _appstore_plan(
    source: MonitorSource,
    fetch_client: FetchClient,
    resolver: RedirectResolver,
    extractor: ArticleExtractor,
) -> SourcePlan:
    return SourcePlan(
        adapters=[AppStoreAdapter(fetch_client)],
        entity=_required_str(source, "app_id"),
        cursor=AppStoreCursor(last_external_id=source.last_external_id),
        library_version=APPSTORE_LIBRARY_VERSION,
    )


def _discourse_plan(
    source: MonitorSource,
    fetch_client: FetchClient,
    resolver: RedirectResolver,
    extractor: ArticleExtractor,
) -> SourcePlan:
    base_url = _required_str(source, "base_url")
    return SourcePlan(
        adapters=[
            DiscourseAdapter(fetch_client, base_url=base_url, query=query)
            for query in _required_queries(source)
        ],
        entity=source.instance_key,
        cursor=DiscourseCursor(last_published_at=source.last_published_at),
        library_version=DISCOURSE_LIBRARY_VERSION,
    )


def _news_plan(
    source: MonitorSource,
    fetch_client: FetchClient,
    resolver: RedirectResolver,
    extractor: ArticleExtractor,
) -> SourcePlan:
    return SourcePlan(
        adapters=[
            NewsAdapter(fetch_client, query=query, resolver=resolver, extractor=extractor)
            for query in _required_queries(source)
        ],
        entity=source.instance_key,
        cursor=NewsCursor(last_published_at=source.last_published_at),
        library_version=NEWS_LIBRARY_VERSION,
    )


def _rss_plan(
    source: MonitorSource,
    fetch_client: FetchClient,
    resolver: RedirectResolver,
    extractor: ArticleExtractor,
) -> SourcePlan:
    return SourcePlan(
        adapters=[FeedAdapter(fetch_client, feed_url=_required_str(source, "feed_url"))],
        entity=source.instance_key,
        cursor=FeedCursor(last_published_at=source.last_published_at),
        library_version=RSS_LIBRARY_VERSION,
    )


def _hackernews_plan(
    source: MonitorSource,
    fetch_client: FetchClient,
    resolver: RedirectResolver,
    extractor: ArticleExtractor,
) -> SourcePlan:
    return SourcePlan(
        adapters=[
            HackerNewsAdapter(fetch_client, query=query) for query in _required_queries(source)
        ],
        entity=source.instance_key,
        cursor=HackerNewsCursor(last_published_at=source.last_published_at),
        library_version=HACKERNEWS_LIBRARY_VERSION,
    )


_PlanBuilder = Callable[[MonitorSource, FetchClient, RedirectResolver, ArticleExtractor], SourcePlan]

_PLAN_BUILDERS: dict[SourceName, _PlanBuilder] = {
    SourceName.PLAY: _play_plan,
    SourceName.APPSTORE: _appstore_plan,
    SourceName.DISCOURSE: _discourse_plan,
    SourceName.NEWS: _news_plan,
    SourceName.RSS: _rss_plan,
    SourceName.HACKERNEWS: _hackernews_plan,
}


def items_from(source_name: SourceName, payload: Any) -> list[CollectedItem]:
    """Normalize one adapter's ``CollectionResult`` into the shared item shape.

    The six payloads agree on neither their fields nor even the name of their
    item list — ``reviews``, ``posts``, ``articles``, ``items`` — so this and
    ``plan_for`` are the only two places a source kind is visible (learn.md,
    "Anti-corruption layer").

    A field the kind does not carry stays ``None``. A zero or an empty string
    would record a measurement the source never made, and an absent rating and
    a rating of zero are different facts (requirement 4, C5).

    As with ``plan_for``, a ``SourceName`` with no mapper here is a bug rather
    than a domain outcome, so the lookup is left to raise.
    """
    return _ITEM_MAPPERS[source_name](payload)


def _review_items(payload: PlayResult | AppStoreResult) -> list[CollectedItem]:
    """Both app stores expose the same review fields under the same names, so
    one mapper serves both kinds; they are also the only two kinds carrying a
    rating or an application version at all.

    ``author_handle`` is the raw username: hashing belongs to the document row,
    not here (C11, design.md "An author hash for a source with no author").
    """
    return [
        CollectedItem(
            external_id=review.external_id,
            author_handle=review.username,
            title=None,
            body=review.content,
            published_at=review.published_at,
            url=None,
            rating=review.rating,
            app_version=review.app_version,
            engagement=None,
        )
        for review in payload.reviews
    ]


def _discourse_items(payload: DiscourseResult) -> list[CollectedItem]:
    """A search hit carries the post's blurb and its like count, but no title
    and no link of its own."""
    return [
        CollectedItem(
            external_id=post.external_id,
            author_handle=post.username,
            title=None,
            body=post.content,
            published_at=post.published_at,
            url=None,
            rating=None,
            app_version=None,
            engagement=post.engagement,
        )
        for post in payload.posts
    ]


# A host can contain neither a colon nor a space, so this cannot collide with a
# real outlet, and an item whose URL has no host still needs a handle because
# documents.author_hash is not nullable.
_UNKNOWN_OUTLET = "outlet:unknown"


def _outlet_handle(url: str | None) -> str:
    """The outlet host of ``url``, standing in for the author a news or RSS item
    does not carry (design.md, "An author hash for a source with no author").

    A leading ``www.`` is stripped so one outlet does not read as two authors and
    understate C6's single-author concentration penalty. The result is the raw
    handle; ``hash_author`` is applied where the document row is built (C11).
    """
    if not url:
        return _UNKNOWN_OUTLET
    try:
        # urlsplit().hostname is already lowercased and free of any port and
        # userinfo, so the www. prefix is all that is left to normalize.
        host = urlsplit(url).hostname
    except ValueError:
        return _UNKNOWN_OUTLET
    return host.removeprefix("www.") if host else _UNKNOWN_OUTLET


def _news_items(payload: NewsResult) -> list[CollectedItem]:
    """An article carries no per-item author, so the outlet host of its resolved
    URL stands in as the handle (design.md, "An author hash for a source with no
    author"). Derived per item, because different articles answering one query
    legitimately come from different outlets."""
    return [
        CollectedItem(
            external_id=article.external_id,
            author_handle=_outlet_handle(article.resolved_url),
            title=article.title,
            body=article.text,
            published_at=article.published_at,
            url=article.resolved_url,
            rating=None,
            app_version=None,
            engagement=None,
        )
        for article in payload.articles
    ]


def _rss_items(payload: FeedResult) -> list[CollectedItem]:
    """A feed entry has no per-item author either, so the outlet host of its own
    link stands in as the handle, and its ``summary`` is the only text it offers.

    Every entry in a feed is the same outlet, so the feed's own host would be the
    right fallback for an entry with no link — but ``CollectionResult`` does not
    carry the feed URL and ``FeedAdapter`` keeps it private, so an entry with no
    link falls back to the unknown-outlet handle instead.
    """
    return [
        CollectedItem(
            external_id=item.external_id,
            author_handle=_outlet_handle(item.url),
            title=item.title,
            body=item.content,
            published_at=item.published_at,
            url=item.url,
            rating=None,
            app_version=None,
            engagement=None,
        )
        for item in payload.items
    ]


def _hackernews_items(payload: HackerNewsResult) -> list[CollectedItem]:
    """A story carries a title, a link, an author and a score but no text of its
    own, so ``body`` is absent for every item of this kind — the title stands in
    for it downstream (design.md, "``body`` falls back to the title")."""
    return [
        CollectedItem(
            external_id=item.external_id,
            author_handle=item.author,
            title=item.title,
            body=None,
            published_at=item.published_at,
            url=item.url,
            rating=None,
            app_version=None,
            engagement=item.engagement,
        )
        for item in payload.items
    ]


_ItemMapper = Callable[[Any], list[CollectedItem]]

_ITEM_MAPPERS: dict[SourceName, _ItemMapper] = {
    SourceName.PLAY: _review_items,
    SourceName.APPSTORE: _review_items,
    SourceName.DISCOURSE: _discourse_items,
    SourceName.NEWS: _news_items,
    SourceName.RSS: _rss_items,
    SourceName.HACKERNEWS: _hackernews_items,
}
