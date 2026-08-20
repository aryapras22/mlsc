"""Fans out one collection task per enabled source and finalises the run.

Each collection outcome is captured as a value, never raised, so one failing
source cannot abort the fan-out (design.md, "Failure strategy").
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.application.runs import RunService, SourceOutcome, SourceResult
from mlsc.core.fetch.client import FetchClient
from mlsc.db.models import SourceName
from mlsc.tasks.ingest import SourceDisabled, collect_play_reviews


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
        outcomes.append(await _collect_one(session_factory, fetch_client, run_id, source))

    await run_service.finalise(run_id, outcomes, expected_volume=bool(sources))


async def _collect_one(session_factory, fetch_client, run_id, source) -> SourceOutcome:  # noqa: ANN001
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
