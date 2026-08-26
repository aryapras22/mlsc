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

from mlsc.core.fetch.client import FetchClient
from mlsc.db.models import MonitorSource, SourceName
from mlsc.sources.appstore import LIBRARY_VERSION as APPSTORE_LIBRARY_VERSION
from mlsc.sources.appstore import AppStoreAdapter, AppStoreCursor
from mlsc.sources.base import SourceAdapter
from mlsc.sources.discourse import LIBRARY_VERSION as DISCOURSE_LIBRARY_VERSION
from mlsc.sources.discourse import DiscourseAdapter, DiscourseCursor
from mlsc.sources.hackernews import LIBRARY_VERSION as HACKERNEWS_LIBRARY_VERSION
from mlsc.sources.hackernews import HackerNewsAdapter, HackerNewsCursor
from mlsc.sources.news.adapter import NewsAdapter, NewsCursor

# The news adapter module declares no version of its own: what it returns is
# trafilatura's extracted text, so trafilatura is the library whose upgrade can
# change the payload, and therefore the one worth recording in the ledger.
from mlsc.sources.news.extract import LIBRARY_VERSION as NEWS_LIBRARY_VERSION
from mlsc.sources.news.extract import ArticleExtractor
from mlsc.sources.news.resolve import RedirectResolver
from mlsc.sources.play import LIBRARY_VERSION as PLAY_LIBRARY_VERSION
from mlsc.sources.play import PlayAdapter, PlayCursor
from mlsc.sources.rss import LIBRARY_VERSION as RSS_LIBRARY_VERSION
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
