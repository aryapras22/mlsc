"""Fans out one collection task per enabled source and finalises the run.

Each collection outcome is captured as a value, never raised, so one failing
source cannot abort the fan-out (design.md, "Failure strategy").
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.application.runs import RunService, SourceOutcome, SourceResult
from mlsc.core.fetch.client import FetchClient
from mlsc.db.models import RunStatus, SourceName
from mlsc.tasks.ingest import SourceDisabled, collect_play_reviews
from mlsc.tasks.maintenance import evaluate_source_health


async def dispatch_run(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    run_service: RunService,
    fetch_client: FetchClient,
    run_id: uuid.UUID,
    monitor_id: uuid.UUID,
) -> None:
    sources = await run_service.enabled_sources(monitor_id)

    outcomes: list[SourceOutcome] = []
    for source in sources:
        outcomes.append(await collect_one_source(session_factory, fetch_client, run_id, source))

    status = await run_service.finalise(run_id, outcomes, expected_volume=bool(sources))
    await evaluate_source_health(session_factory, run_id)

    if status in (RunStatus.COMPLETE, RunStatus.PARTIAL):
        await _run_downstream_pipeline(session_factory, run_id=run_id, monitor_id=monitor_id)


async def _run_downstream_pipeline(
    session_factory: async_sessionmaker[AsyncSession], *, run_id: uuid.UUID, monitor_id: uuid.UUID
) -> None:
    """Enrichment, topic assignment, the metrics rollup, trend detection and
    scoring, alerting, and insight generation, in that order — each stage's
    output is the next stage's input (design.md's "Success path" for
    `document-enrichment`, `persistent-topics`, `daily-topic-metrics`,
    `trend-detection`, `metrics-read-api`, and `insight-generation`
    respectively). A stage failing here does not revisit run classification:
    the run itself already collected successfully, and a downstream failure
    is retried by re-running this pipeline rather than by re-collecting.

    Insight generation is the one step tolerated to be missing: not every
    environment has an LLM tier configured, and that must degrade the
    sequence rather than crash it (requirement 2), matching
    `mlsc.run_override`'s existing tolerance for the same condition.
    """
    from mlsc.config import ConfigurationError, load_trend_detection_settings
    from mlsc.db.models import IngestionRun, Monitor, TargetType
    from mlsc.llm.router import LlmRouter
    from mlsc.pipeline.enrich import Embedder, SentimentScorer
    from mlsc.tasks.alerts import evaluate_alerts
    from mlsc.tasks.analytics import detect_trends, run_daily_analytics, score_trends
    from mlsc.tasks.enrich import ALL_STAGES, enrich_documents
    from mlsc.tasks.insights import generate_insights
    from mlsc.tasks.themes import build_theme_relevance_context

    async with session_factory() as session:
        run = await session.get(IngestionRun, run_id)
        run_date = run.run_date
        monitor = await session.get(Monitor, monitor_id)
        is_theme = monitor.target_type is TargetType.THEME

    embedder = Embedder()
    theme_relevance = (
        await build_theme_relevance_context(session_factory, monitor_id=monitor_id, embedder=embedder)
        if is_theme
        else None
    )

    await enrich_documents(
        session_factory,
        monitor_id=monitor_id,
        stages=ALL_STAGES,
        embedder=embedder,
        sentiment_scorer=SentimentScorer(),
        theme_relevance=theme_relevance,
    )
    await run_daily_analytics(session_factory, monitor_id=monitor_id, run_date=run_date)

    trend_settings = load_trend_detection_settings()
    await detect_trends(session_factory, monitor_id=monitor_id, bucket=run_date, settings=trend_settings)
    await score_trends(session_factory, monitor_id=monitor_id, bucket=run_date, settings=trend_settings)
    await evaluate_alerts(session_factory, monitor_id=monitor_id, bucket=run_date)

    try:
        llm_router = LlmRouter.from_configuration()
    except ConfigurationError:
        return

    await generate_insights(
        session_factory,
        monitor_id=monitor_id,
        period_start=run_date,
        period_end=run_date,
        llm_router=llm_router,
    )


async def collect_one_source(session_factory, fetch_client, run_id, source) -> SourceOutcome:  # noqa: ANN001
    if source.source_name is not SourceName.PLAY:
        return SourceOutcome(
            source_id=source.id,
            source_name=source.source_name.value,
            result=SourceResult.SKIPPED_DISABLED,
            error="only the play adapter is implemented",
        )

    try:
        stats = await collect_play_reviews(
            session_factory=session_factory,
            fetch_client=fetch_client,
            run_id=run_id,
            source_id=source.id,
        )
    except SourceDisabled:
        return SourceOutcome(
            source_id=source.id, source_name=source.source_name.value,
            result=SourceResult.SKIPPED_DISABLED,
        )

    if stats.validation_failed:
        result = SourceResult.FAILED_VALIDATION
    elif stats.error:
        result = SourceResult.FAILED_TRANSPORT
    else:
        result = SourceResult.COLLECTED

    return SourceOutcome(
        source_id=source.id,
        source_name=source.source_name.value,
        result=result,
        kept=stats.kept,
        error=stats.error,
    )
