"""Sample-context loading: per source and bucket, how big a sample the
figures for that day are computed over, and whether the source was
truncated, measured, or produced nothing at all.

The denominator is a property of the source and the day, not of the topic —
computing it per topic row would let two topics on one day disagree about
the sample they came from (design.md, "Domain shapes": ``SampleContext``).
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import (
    Document,
    FetchStats,
    IngestionRun,
    MonitorSource,
    QuotaOutcome,
    SourceName,
)
from mlsc.pipeline.analytics.buckets import bucket_range_utc


class LedgerMissing(RuntimeError):
    """Documents exist for a source and bucket, but no statistics row does.

    An invariant violation, not an ordinary absence: every collection writes
    its ledger row on every outcome including failure (design.md, "Failure
    strategy" — this crashes)."""


class SampleZero(RuntimeError):
    """A source's ledger says nothing was kept, but documents exist for it
    anyway. The ledger and the documents disagree, which is a bug upstream
    (design.md, "Failure strategy" — this crashes)."""


class SampleOutcome(str, Enum):
    MEASURED = "measured"
    TRUNCATED = "truncated"
    ABSENT = "absent"


@dataclasses.dataclass(frozen=True)
class SampleContext:
    source_name: SourceName
    bucket: date
    sample_size: int
    quota_hit: bool
    outcome: SampleOutcome


@dataclasses.dataclass(frozen=True)
class AbsentSource:
    """A source that gets no row for this bucket, and why. Requirement 4
    says the absence is silent in the metrics table; it is not silent here —
    the rollup task logs this so an absent source is diagnosable rather than
    merely invisible."""

    source_name: SourceName
    reason: str


@dataclasses.dataclass(frozen=True)
class BucketSampleContext:
    contexts: list[SampleContext]
    absent: list[AbsentSource]


async def load_context(
    session: AsyncSession, monitor_id: uuid.UUID, bucket: date, *, timezone: str
) -> BucketSampleContext:
    """One ``SampleContext`` per source with a usable sample for ``bucket``,
    plus an ``AbsentSource`` for every attached, enabled source that kept
    nothing that day.

    A source is absent — and gets no row at all, per requirement 4 —
    whenever it kept nothing that day, whatever the reason: it failed, was
    skipped, or genuinely ran and found nothing new. A sample of size zero
    has no denominator a share could be computed against, so it is withheld
    the same way a failed source is, rather than being written as a zero.
    """
    bucket_start, bucket_end = bucket_range_utc(bucket, timezone=timezone)

    # A scheduled run and a backfill run can both target the same date, so
    # every run for the date is summed rather than assuming exactly one.
    ledger_result = await session.execute(
        select(
            MonitorSource.source_name,
            FetchStats.kept,
            FetchStats.quota_outcome,
            FetchStats.outcome_kind,
            FetchStats.error,
        )
        .join(IngestionRun, IngestionRun.id == FetchStats.run_id)
        .join(MonitorSource, MonitorSource.id == FetchStats.monitor_source_id)
        .where(IngestionRun.monitor_id == monitor_id, IngestionRun.run_date == bucket)
    )

    kept_by_source: dict[SourceName, int] = {}
    quota_hit_by_source: dict[SourceName, bool] = {}
    absent_reason_by_source: dict[SourceName, str] = {}
    for source_name, kept, quota_outcome, outcome_kind, error in ledger_result.all():
        kept_by_source[source_name] = kept_by_source.get(source_name, 0) + kept
        quota_hit_by_source[source_name] = quota_hit_by_source.get(
            source_name, False
        ) or quota_outcome is QuotaOutcome.ALLOWANCE_REACHED
        if error:
            absent_reason_by_source[source_name] = error
        else:
            absent_reason_by_source.setdefault(source_name, outcome_kind.value)

    documents_result = await session.execute(
        select(Document.source_name)
        .where(
            Document.monitor_id == monitor_id,
            Document.published_at >= bucket_start,
            Document.published_at < bucket_end,
        )
        .distinct()
    )
    sources_with_documents = {row[0] for row in documents_result.all()}

    for source_name in sources_with_documents:
        if source_name not in kept_by_source:
            raise LedgerMissing(f"{source_name.value} has documents for {bucket} but no ledger row")
        if kept_by_source[source_name] == 0:
            raise SampleZero(f"{source_name.value} ledger reports zero kept for {bucket} but documents exist")

    contexts: list[SampleContext] = []
    absent: list[AbsentSource] = []
    for source_name, kept in kept_by_source.items():
        if kept == 0:
            absent.append(
                AbsentSource(
                    source_name=source_name,
                    reason=absent_reason_by_source.get(source_name, "no_rows_kept"),
                )
            )
            continue  # no usable sample, write nothing (requirement 4)
        quota_hit = quota_hit_by_source[source_name]
        contexts.append(
            SampleContext(
                source_name=source_name,
                bucket=bucket,
                sample_size=kept,
                quota_hit=quota_hit,
                outcome=SampleOutcome.TRUNCATED if quota_hit else SampleOutcome.MEASURED,
            )
        )

    return BucketSampleContext(contexts=contexts, absent=absent)
