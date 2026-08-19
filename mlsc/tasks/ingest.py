"""Play review collection: fetch, hash, insert, and always write the ledger row.

Advances the cursor only to the newest row actually persisted, never to the
newest row merely seen — a write failure must not skip real reviews on the
next run (design.md, "Failure strategy").
"""

from __future__ import annotations

import time
import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.core.fetch.client import FetchClient
from mlsc.db.models import FetchStats, MonitorSource, QuotaOutcome
from mlsc.pipeline.normalize import hash_author, hash_content
from mlsc.repositories.documents import DocumentRepository
from mlsc.repositories.sources import MonitorSourceRepository
from mlsc.sources.play import LIBRARY_VERSION, PlayAdapter, PlayCollectionFailed, PlayCursor


class SourceDisabled(RuntimeError):
    """Raised when the configured source exists but is switched off."""


class Clock(Protocol):
    def monotonic(self) -> float: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


async def collect_play_reviews(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    fetch_client: FetchClient,
    run_id: uuid.UUID,
    source_id: uuid.UUID,
    clock: Clock | None = None,
) -> FetchStats:
    """Collect one source's reviews for one run, writing exactly one stats row.

    The stats row is written on every outcome, including a validation or
    transport failure — a run without its ledger row is worse than a failed
    run, because every later surface would treat the gap as a measurement
    (design.md, "Failure strategy").
    """
    clock = clock or _SystemClock()
    started_at = clock.monotonic()

    async with session_factory() as session:
        source = await MonitorSourceRepository(session).get(source_id)
        if not source.enabled:
            raise SourceDisabled(str(source_id))
        package_id = source.config["package_id"]
        cursor = PlayCursor(
            last_external_id=source.last_external_id,
            last_published_at=source.last_published_at,
        )
        quota = source.daily_quota

    adapter = PlayAdapter(fetch_client)

    try:
        result = await adapter.fetch(package_id, cursor, quota)
    except PlayCollectionFailed as failure:
        duration = clock.monotonic() - started_at
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
            validation_failed=failure.status.value == "validation_failed",
            library_version=LIBRARY_VERSION,
            duration_seconds=duration,
            error=str(failure),
        )

    rows = [
        dict(
            id=uuid.uuid4(),
            monitor_id=source.monitor_id,
            source_name=source.source_name,
            external_id=review.external_id,
            entity_id=package_id,
            url=None,
            author_hash=hash_author(review.username),
            body=review.content,
            published_at=review.published_at,
            rating=review.rating,
            app_version=review.app_version,
            engagement=None,
            content_hash=hash_content(review.content, str(review.rating)),
            raw={},
        )
        for review in result.reviews
    ]

    async with session_factory() as session:
        kept = await DocumentRepository(session).insert_ignoring_duplicates(rows)
        if result.reviews:
            await MonitorSourceRepository(session).save_cursor(
                source_id,
                last_external_id=result.new_cursor.last_external_id,
                last_published_at=result.new_cursor.last_published_at,
            )
        await session.commit()

    duration = clock.monotonic() - started_at
    attempted = len(result.reviews)
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
            QuotaOutcome.ALLOWANCE_REACHED if result.quota_reached else QuotaOutcome.WITHIN_ALLOWANCE
        ),
        validation_failed=False,
        library_version=LIBRARY_VERSION,
        duration_seconds=duration,
        error=None,
    )


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
