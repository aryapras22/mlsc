"""Assembles one ``DataQuality`` block per range, from the same
source-statistics rows every time (design.md, "Domain shapes") — the
mechanism behind requirement 2: an endpoint that built its own block would
eventually build a wrong one (learn.md, "The data-quality block belongs to
the wrapper, not the endpoint").
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import DailyMetric, FetchStats, IngestionRun, MonitorSource, SourceName
from mlsc.schemas.metrics import DataQuality


async def assemble(session: AsyncSession, *, monitor_id: uuid.UUID, start: date, end: date) -> DataQuality:
    """Requirement 2: sample size, truncated days, and which sources
    succeeded or failed over the range — computed once here so every
    response that carries a quality block carries the same one.
    """
    monitor_level_result = await session.execute(
        select(DailyMetric.bucket, DailyMetric.sample_size, DailyMetric.quota_hit).where(
            DailyMetric.monitor_id == monitor_id, DailyMetric.source_name.is_(None),
            DailyMetric.topic_id.is_(None), DailyMetric.bucket >= start, DailyMetric.bucket <= end,
        )
    )
    monitor_level_rows = monitor_level_result.all()
    sample_size = sum(row.sample_size for row in monitor_level_rows)
    truncated_days = sorted({row.bucket for row in monitor_level_rows if row.quota_hit})

    attached_result = await session.execute(
        select(MonitorSource.id, MonitorSource.source_name).where(MonitorSource.monitor_id == monitor_id)
    )
    attached_sources = {row.id: row.source_name for row in attached_result.all()}

    stats_result = await session.execute(
        select(FetchStats.monitor_source_id, FetchStats.validation_failed, FetchStats.error)
        .join(IngestionRun, IngestionRun.id == FetchStats.run_id)
        .where(
            IngestionRun.monitor_id == monitor_id,
            IngestionRun.run_date >= start, IngestionRun.run_date <= end,
        )
    )
    failed_source_ids = {
        row.monitor_source_id for row in stats_result.all() if row.validation_failed or row.error
    }

    sources_ok: list[SourceName] = []
    sources_failed: list[SourceName] = []
    for source_id, source_name in attached_sources.items():
        (sources_failed if source_id in failed_source_ids else sources_ok).append(source_name)

    return DataQuality(
        sample_size=sample_size, truncated_days=truncated_days,
        sources_ok=sources_ok, sources_failed=sources_failed, topics_absent=[],
    )
