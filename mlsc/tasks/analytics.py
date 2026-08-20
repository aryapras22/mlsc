"""The daily rollup task: turns one bucket's enriched, assigned documents into
its metrics rows, for every bucket a caller names.

Takes a bucket set rather than a single date, because any bucket can need
recomputing at any time — late arrival is the norm here, not the exception
(design.md, "Dependencies, injected"; learn.md, "Late arrival is normal").
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import Assignment, Document, Enrichment, Monitor, RollupReason
from mlsc.pipeline.analytics.buckets import bucket_for, bucket_range_utc
from mlsc.pipeline.analytics.group import rollup_bucket
from mlsc.pipeline.analytics.normalization import load_context
from mlsc.pipeline.analytics.rollup import row_from
from mlsc.repositories.metrics import MetricRepository

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class BucketOutcome:
    bucket: date
    written: int
    pruned: int
    absent_sources: int


@dataclasses.dataclass(frozen=True)
class RollupOutcome:
    completed: list[BucketOutcome]
    failed: list[tuple[date, str]]


async def rollup_daily(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    buckets: list[date],
    reason: RollupReason,
) -> RollupOutcome:
    """Recompute every bucket in ``buckets`` independently, completing the
    others when one fails (design.md, "Failure strategy": "Partial bucket
    failure inside a multi-bucket request")."""
    completed: list[BucketOutcome] = []
    failed: list[tuple[date, str]] = []

    for bucket in buckets:
        try:
            outcome = await _rollup_one_bucket(
                session_factory, monitor_id=monitor_id, bucket=bucket, reason=reason
            )
        except Exception as error:  # noqa: BLE001 - one bad bucket must not abort the batch
            logger.exception("rollup failed for monitor %s bucket %s", monitor_id, bucket)
            failed.append((bucket, str(error)))
            continue
        completed.append(outcome)

    return RollupOutcome(completed=completed, failed=failed)


async def _rollup_one_bucket(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    bucket: date,
    reason: RollupReason,
) -> BucketOutcome:
    async with session_factory() as session:
        monitor = await session.get(Monitor, monitor_id)
        timezone = monitor.timezone

        sample = await load_context(session, monitor_id, bucket, timezone=timezone)
        for absent in sample.absent:
            logger.info(
                "source %s absent for monitor %s bucket %s: %s",
                absent.source_name.value, monitor_id, bucket, absent.reason,
            )

        contexts_by_source = {context.source_name: context for context in sample.contexts}
        rows_by_source_topic = await _load_grouped_rows(session, monitor_id, bucket, timezone=timezone)

        grouped = rollup_bucket(rows_by_source_topic, contexts_by_source)

        repo = MetricRepository(session)
        await repo.upsert_bucket(monitor_id=monitor_id, bucket=bucket, rows=grouped, reason=reason)

        surviving_keys = {
            (row.source_name.value if row.source_name else None, row.topic_id) for row in grouped
        }
        pruned = await repo.prune_unsupported(
            monitor_id=monitor_id, bucket=bucket, surviving_keys=surviving_keys
        )

        await session.commit()

    return BucketOutcome(
        bucket=bucket, written=len(grouped), pruned=pruned, absent_sources=len(sample.absent)
    )


async def run_daily_analytics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    run_date: date,
) -> RollupOutcome:
    """The daily sequence's tail: assign today's enriched documents to
    topics, then roll up ``run_date`` — registered after topic assignment
    because a document's topic (or lack of one) is exactly what the
    by-topic breakdown groups on (requirement 1, 6; design.md, "Success
    path").

    Only ``run_date`` is rolled up here, not every bucket a source's
    documents happen to be published on: the sample context a bucket needs
    (``SampleContext``) comes from the ``FetchStats`` ledger keyed to
    ``IngestionRun.run_date``, and a cursor-based source's page can legally
    return an item published days before the run that collected it. Such an
    item is not a late arrival in the requirement-6 sense — the run that
    collected it is exactly today's run — it simply is not the sample this
    layer has a denominator for. It is correctly folded into ``run_date``'s
    own figures by ``_load_grouped_rows``, which groups by publish bucket
    inside the range this call rolls up, not implicitly claimed by a bucket
    with no run of its own.

    Discovery, refit and dormancy are not called here: they run weekly and
    monthly respectively (design.md, "Success path": "Three graphs on three
    cadences"), on their own schedule rather than this daily one.
    """
    from mlsc.config import load_topic_thresholds
    from mlsc.tasks.topics import assign_topics

    await assign_topics(
        session_factory, monitor_id=monitor_id, thresholds=load_topic_thresholds(), today=run_date
    )
    return await rollup_daily(
        session_factory, monitor_id=monitor_id, buckets=[run_date], reason=RollupReason.SCHEDULED
    )


async def recompute_affected(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    document_ids: list[uuid.UUID],
) -> RollupOutcome:
    """Requirement 6: a late-arriving document recomputes only the buckets it
    actually falls in, not the whole series (design.md, "Success path":
    ``recompute_affected``).
    """
    if not document_ids:
        return RollupOutcome(completed=[], failed=[])

    async with session_factory() as session:
        monitor = await session.get(Monitor, monitor_id)
        timezone = monitor.timezone

        result = await session.execute(
            select(Document.published_at).where(Document.id.in_(document_ids))
        )
        published_ats = [row[0] for row in result.all()]

    buckets = sorted({bucket_for(instant, timezone=timezone) for instant in published_ats})
    return await rollup_daily(
        session_factory, monitor_id=monitor_id, buckets=buckets, reason=RollupReason.LATE_ARRIVAL
    )


async def _load_grouped_rows(
    session: AsyncSession, monitor_id: uuid.UUID, bucket: date, *, timezone: str
) -> dict[tuple, list]:
    bucket_start, bucket_end = bucket_range_utc(bucket, timezone=timezone)
    result = await session.execute(
        select(Document, Enrichment, Assignment.topic_id)
        .join(Enrichment, Enrichment.document_id == Document.id)
        .outerjoin(Assignment, Assignment.document_id == Document.id)
        .where(
            Document.monitor_id == monitor_id,
            Document.published_at >= bucket_start,
            Document.published_at < bucket_end,
            Enrichment.is_relevant.is_(True),
        )
    )

    grouped: dict[tuple, list] = {}
    for document, enrichment, topic_id in result.all():
        key = (document.source_name, topic_id)
        grouped.setdefault(key, []).append(row_from(document, enrichment))
    return grouped
