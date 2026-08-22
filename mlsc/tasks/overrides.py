"""Runs one operator-initiated repair job to completion.

Each branch writes ``outcome`` on every path, including failure —
requirement 7 exists because a job that failed partway and reported
nothing is worse than one that never ran (design.md, "Success path").
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.application.backfill import BackfillService
from mlsc.application.runs import SourceOutcome, SourceResult
from mlsc.core.fetch.client import FetchClient
from mlsc.db.models import (
    Document,
    IngestionRun,
    Monitor,
    MonitorSource,
    OverrideJob,
    OverrideKind,
    OverrideStatus,
    RollupReason,
    TargetType,
)
from mlsc.llm.router import LlmRouter
from mlsc.pipeline.analytics.buckets import bucket_for
from mlsc.pipeline.enrich import Embedder, SentimentScorer
from mlsc.pipeline.stages import Stage
from mlsc.tasks.analytics import rollup_daily
from mlsc.tasks.dispatch import collect_one_source
from mlsc.tasks.enrich import enrich_documents
from mlsc.tasks.retention import enforce_retention

_COLLECTION_FAILURES = (SourceResult.FAILED_VALIDATION, SourceResult.FAILED_TRANSPORT)


async def run_override(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: uuid.UUID,
    fetch_client: FetchClient,
    embedder: Embedder,
    sentiment_scorer: SentimentScorer,
    llm_router: LlmRouter | None = None,
) -> None:
    async with session_factory() as session:
        job = await session.get(OverrideJob, job_id)
        job.status = OverrideStatus.RUNNING
        await session.commit()
        monitor_id, kind, parameters = job.monitor_id, job.kind, job.parameters

    if kind is OverrideKind.STAGE_RERUN:
        status, outcome = await _run_stage_rerun(
            session_factory,
            monitor_id=monitor_id,
            stage=Stage(parameters["stage"]),
            embedder=embedder,
            sentiment_scorer=sentiment_scorer,
            llm_router=llm_router,
        )
    elif kind is OverrideKind.BACKFILL_WINDOW:
        status, outcome = await _run_backfill_window(
            session_factory,
            fetch_client,
            monitor_id=monitor_id,
            window_start=date.fromisoformat(parameters["window_start"]),
            window_end=date.fromisoformat(parameters["window_end"]),
        )
    else:
        status, outcome = await _run_retention_purge(session_factory, monitor_id=monitor_id)

    async with session_factory() as session:
        job = await session.get(OverrideJob, job_id)
        job.status = status
        job.outcome = outcome
        job.finished_at = datetime.now(timezone.utc)
        await session.commit()


async def _run_stage_rerun(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    stage: Stage,
    embedder: Embedder,
    sentiment_scorer: SentimentScorer,
    llm_router: LlmRouter | None = None,
) -> tuple[OverrideStatus, dict[str, Any]]:
    async with session_factory() as session:
        monitor = await session.get(Monitor, monitor_id)

    theme_relevance = None
    if monitor.target_type is TargetType.THEME and stage is Stage.RELEVANCE:
        from mlsc.tasks.themes import build_theme_relevance_context

        theme_relevance = await build_theme_relevance_context(
            session_factory, monitor_id=monitor_id, embedder=embedder
        )

    try:
        written = await enrich_documents(
            session_factory,
            monitor_id=monitor_id,
            stages=frozenset({stage}),
            embedder=embedder,
            sentiment_scorer=sentiment_scorer,
            llm_router=llm_router,
            theme_relevance=theme_relevance,
        )
    except Exception as error:  # noqa: BLE001 - recorded as the job's outcome, not raised further
        return OverrideStatus.FAILED, {"stage": stage.value, "error": str(error)}

    async with session_factory() as session:
        result = await session.execute(
            select(Document.published_at).where(Document.monitor_id == monitor_id)
        )
        affected_dates = sorted({bucket_for(row[0], timezone=monitor.timezone) for row in result.all()})

    try:
        rollup_outcome = await rollup_daily(
            session_factory, monitor_id=monitor_id, buckets=affected_dates, reason=RollupReason.MANUAL
        )
    except Exception as error:  # noqa: BLE001 - same as above, this is the job's outcome
        return OverrideStatus.FAILED, {
            "stage": stage.value,
            "documents_reenriched": written,
            "recompute_error": str(error),
        }

    if rollup_outcome.failed:
        # A bucket whose recompute failed is stale metrics next to fresh
        # enrichment — internally inconsistent and indistinguishable from a
        # clean day, which is the C5 failure design.md names (design.md,
        # "Failure strategy"). Partial is not offered here; the backfill
        # branch's partial tolerance does not apply to a shared aggregate.
        return OverrideStatus.FAILED, {
            "stage": stage.value,
            "documents_reenriched": written,
            "dates_recomputed": [outcome.bucket.isoformat() for outcome in rollup_outcome.completed],
            "recompute_failures": [
                {"bucket": bucket.isoformat(), "error": error} for bucket, error in rollup_outcome.failed
            ],
        }

    return OverrideStatus.COMPLETE, {
        "stage": stage.value,
        "documents_reenriched": written,
        "dates_recomputed": [bucket.isoformat() for bucket in affected_dates],
    }


async def _run_backfill_window(
    session_factory: async_sessionmaker[AsyncSession],
    fetch_client: FetchClient,
    *,
    monitor_id: uuid.UUID,
    window_start: date,
    window_end: date,
) -> tuple[OverrideStatus, dict[str, Any]]:
    backfill_service = BackfillService(session_factory)
    backfill_job_id = await backfill_service.submit(monitor_id, window_start, window_end)

    outcomes: list[tuple[date, SourceOutcome]] = []

    async def collect_one_date(run_id: uuid.UUID, source_id: uuid.UUID) -> None:
        async with session_factory() as session:
            run = await session.get(IngestionRun, run_id)
            source = await session.get(MonitorSource, source_id)
        try:
            outcome = await collect_one_source(session_factory, fetch_client, run_id, source)
        except Exception as error:  # noqa: BLE001 - recorded, not raised: one date must not abort the window
            outcome = SourceOutcome(
                source_id=source.id,
                source_name=source.source_name.value,
                result=SourceResult.FAILED_TRANSPORT,
                error=str(error),
            )
        outcomes.append((run.run_date, outcome))

    await backfill_service.run(backfill_job_id, collect_one_date=collect_one_date)

    any_collected = any(o.result is SourceResult.COLLECTED for _, o in outcomes)
    failures = [(d, o) for d, o in outcomes if o.result in _COLLECTION_FAILURES]

    if not outcomes or not any_collected:
        status = OverrideStatus.FAILED
    elif failures:
        status = OverrideStatus.PARTIAL
    else:
        status = OverrideStatus.COMPLETE

    return status, {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "documents_kept": sum(o.kept for _, o in outcomes),
        "dates_collected": sorted({d.isoformat() for d, o in outcomes if o.result is SourceResult.COLLECTED}),
        "failures": [
            {"date": d.isoformat(), "source_name": o.source_name, "error": o.error} for d, o in failures
        ],
    }


async def _run_retention_purge(
    session_factory: async_sessionmaker[AsyncSession], *, monitor_id: uuid.UUID
) -> tuple[OverrideStatus, dict[str, Any]]:
    result = await enforce_retention(session_factory, monitor_id)
    return OverrideStatus.COMPLETE, {
        "cutoff": result.cutoff.isoformat(),
        "documents_removed": result.documents_removed,
    }
