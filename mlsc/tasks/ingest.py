"""Source collection: fetch every adapter the plan holds, hash, insert, and
always write the ledger row.

One code path for all six kinds. Only ``plan_for`` and ``items_from`` know what
kind a source is, and everything from the fetch onward is uniform (design.md,
"Success path").

Advances the cursor only as far as the items actually persisted, never as far as
the items merely seen — a write failure must re-collect rather than skip, and a
skipped window is indistinguishable from a quiet day forever after
(requirement 8).
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.core.fetch.client import FetchClient
from mlsc.core.fetch.contracts import FetchStatus
from mlsc.db.models import FetchStats, MonitorSource, QuotaOutcome
from mlsc.pipeline.normalize import hash_author, hash_content
from mlsc.repositories.documents import DocumentRepository
from mlsc.repositories.sources import MonitorSourceRepository
from mlsc.sources.appstore import AppStoreCollectionFailed
from mlsc.sources.collect import CollectedItem, SourceCollectionFailed, items_from, plan_for
from mlsc.sources.discourse import DiscourseCollectionFailed
from mlsc.sources.hackernews import HackerNewsCollectionFailed
from mlsc.sources.news.adapter import NewsCollectionFailed
from mlsc.sources.news.extract import ArticleExtractor
from mlsc.sources.news.resolve import RedirectResolver
from mlsc.sources.play import PlayCollectionFailed
from mlsc.sources.rss import FeedCollectionFailed, MalformedFeed


class SourceDisabled(RuntimeError):
    """Raised when the configured source exists but is switched off."""


# Each adapter raises its own failure type; the ledger records one shape for all
# of them, so this is where the six vocabularies converge.
_ADAPTER_FAILURES = (
    PlayCollectionFailed,
    AppStoreCollectionFailed,
    DiscourseCollectionFailed,
    NewsCollectionFailed,
    HackerNewsCollectionFailed,
    FeedCollectionFailed,
    MalformedFeed,
)

# A stored config that will not build fails before any request, so no library was
# reached and none can honestly be named. Recorded as a word rather than a blank
# because library_version is not nullable and an empty string reads as a bug.
_NO_LIBRARY_REACHED = "none"


class Clock(Protocol):
    def monotonic(self) -> float: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


async def collect_source(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    fetch_client: FetchClient,
    resolver: RedirectResolver,
    extractor: ArticleExtractor,
    run_id: uuid.UUID,
    source_id: uuid.UUID,
    clock: Clock | None = None,
) -> FetchStats:
    """Collect one source's items for one run, writing exactly one stats row.

    A source's whole fan-out runs under its single daily allowance and
    aggregates into that one row. A full allowance per query would multiply the
    source's real request volume by its query count against C13 (design.md, "A
    source's quota caps its whole fan-out").

    The stats row is written on every outcome, including a stored config that
    will not build and a transport failure — a run without its ledger row is
    worse than a failed run, because every later surface would read the gap as a
    measurement (requirement 5, design.md "Failure strategy"). ``SourceDisabled``
    is the one failure that propagates: a disabled source is not part of the run.
    """
    clock = clock or _SystemClock()
    started_at = clock.monotonic()

    async with session_factory() as session:
        source = await MonitorSourceRepository(session).get(source_id)
        if not source.enabled:
            raise SourceDisabled(str(source_id))
        quota = source.daily_quota

    try:
        plan = plan_for(source, fetch_client, resolver, extractor)
    except SourceCollectionFailed as failure:
        return await _write_stats(
            session_factory,
            run_id=run_id,
            source_id=source_id,
            attempted=0,
            fetched=0,
            duplicates=0,
            kept=0,
            quota=quota,
            quota_outcome=QuotaOutcome.WITHIN_ALLOWANCE,
            validation_failed=failure.validation_failed,
            library_version=_NO_LIBRARY_REACHED,
            duration_seconds=clock.monotonic() - started_at,
            error=str(failure),
        )

    items: list[CollectedItem] = []
    new_cursor: Any = None
    remaining = quota
    allowance_reached = False
    validation_failed = False
    error: str | None = None

    for adapter in plan.adapters:
        if remaining <= 0:
            allowance_reached = True
            break
        try:
            result = await adapter.fetch(plan.entity, plan.cursor, remaining)
        except _ADAPTER_FAILURES as failure:
            error = str(failure)
            validation_failed = _is_validation_failure(failure)
            break
        collected = items_from(source.source_name, result)
        items.extend(collected)
        remaining -= len(collected)
        allowance_reached = allowance_reached or result.quota_reached
        new_cursor = _newer_cursor(new_cursor, result.new_cursor)

    rows = _document_rows(source, items)

    async with session_factory() as session:
        kept = await DocumentRepository(session).insert_ignoring_duplicates(rows)
        # A duplicate counts as persisted: the natural key means the row is
        # already in the table, so the cursor may pass it. An error anywhere in
        # the fan-out holds the cursor back entirely, because the adapters share
        # one watermark and advancing it past a failed query's window would skip
        # that query's older items on every run after this one (requirement 8).
        if rows and error is None:
            await MonitorSourceRepository(session).save_cursor(
                source_id, **dataclasses.asdict(new_cursor)
            )
        await session.commit()

    attempted = len(rows)
    return await _write_stats(
        session_factory,
        run_id=run_id,
        source_id=source_id,
        attempted=attempted,
        fetched=attempted,
        duplicates=attempted - kept,
        kept=kept,
        quota=quota,
        quota_outcome=(
            QuotaOutcome.ALLOWANCE_REACHED if allowance_reached else QuotaOutcome.WITHIN_ALLOWANCE
        ),
        validation_failed=validation_failed,
        library_version=plan.library_version,
        duration_seconds=clock.monotonic() - started_at,
        error=error,
    )


def _is_validation_failure(failure: Exception) -> bool:
    """Whether the fault was the payload's shape rather than the transport — the
    distinction ``collect_one_source`` turns into ``FAILED_VALIDATION``.

    ``MalformedFeed`` is the one adapter failure carrying no ``FetchStatus``: the
    fetch itself succeeded and the body was the problem, which is a validation
    failure by definition.
    """
    if isinstance(failure, MalformedFeed):
        return True
    return failure.status is FetchStatus.VALIDATION_FAILED


def _newer_cursor(current: Any, candidate: Any) -> Any:
    """Keep whichever of two cursors sits further forward.

    Ordering is by ``last_published_at``, since a fan-out's adapters share one
    watermark and only that field is comparable; a kind tracking no timestamp —
    App Store — has a single adapter, so its one cursor is the whole fan-out and
    the candidate simply wins.
    """
    if current is None:
        return candidate
    current_at = getattr(current, "last_published_at", None)
    candidate_at = getattr(candidate, "last_published_at", None)
    if current_at is None or (candidate_at is not None and candidate_at > current_at):
        return candidate
    return current


def _document_rows(source: MonitorSource, items: list[CollectedItem]) -> list[dict[str, Any]]:
    """Turn normalized items into document rows, hashing the author and the content.

    ``entity_id`` is the source's ``instance_key`` for every kind: the column is
    not nullable and a query-driven source has no entity, while for Play the key
    is the package id this already wrote (design.md, "``entity_id`` is the
    source's ``instance_key``").

    ``body`` falls back to the title, because enrichment embeds ``body`` and a
    Hacker News story carries only a title and a link (design.md, "``body`` falls
    back to the title"). An item with neither still becomes a row with a null
    body: its rating or engagement count is a measurement in its own right, and
    dropping it would report a real item as a duplicate.
    """
    rows: list[dict[str, Any]] = []
    for item in items:
        body = item.body or item.title or None
        rows.append(
            dict(
                id=uuid.uuid4(),
                monitor_id=source.monitor_id,
                source_name=source.source_name,
                external_id=item.external_id,
                entity_id=source.instance_key,
                url=item.url,
                author_hash=hash_author(item.author_handle),
                body=body,
                published_at=item.published_at,
                rating=item.rating,
                app_version=item.app_version,
                engagement=item.engagement,
                # The rating is stringified only when the kind has one, so this
                # reproduces the hash Play rows were written with while a kind
                # without ratings hashes its text alone.
                content_hash=hash_content(
                    body, str(item.rating) if item.rating is not None else None
                ),
                raw={},
            )
        )
    return rows


async def _write_stats(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    source_id: uuid.UUID,
    attempted: int,
    fetched: int,
    duplicates: int,
    kept: int,
    quota: int,
    quota_outcome: QuotaOutcome,
    validation_failed: bool,
    library_version: str,
    duration_seconds: float,
    error: str | None,
) -> FetchStats:
    async with session_factory() as session:
        stats = FetchStats(
            id=uuid.uuid4(),
            run_id=run_id,
            monitor_source_id=source_id,
            attempted=attempted,
            fetched=fetched,
            duplicates=duplicates,
            kept=kept,
            quota=quota,
            quota_outcome=quota_outcome,
            validation_failed=validation_failed,
            library_version=library_version,
            duration_seconds=duration_seconds,
            error=error,
        )
        session.add(stats)
        await session.commit()
        return stats
