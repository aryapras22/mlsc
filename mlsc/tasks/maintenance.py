"""Health evaluation after a run, and weekly smoke-test maintenance."""

from __future__ import annotations

import statistics
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.application.health import evaluate
from mlsc.db.models import AlertKind, FetchStats, MonitorSource, SourceState
from mlsc.repositories.alerts import ScraperAlertRepository
from mlsc.repositories.health import HealthRepository


async def evaluate_source_health(
    session_factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> None:
    """One verdict per source, from this run's statistics rows."""
    async with session_factory() as session:
        stats_result = await session.execute(
            select(FetchStats).where(FetchStats.run_id == run_id)
        )
        stats_rows = list(stats_result.scalars().all())
        if not stats_rows:
            return

        health_repo = HealthRepository(session)
        alert_repo = ScraperAlertRepository(session)

        for stats in stats_rows:
            history_result = await session.execute(
                select(FetchStats.kept)
                .where(FetchStats.monitor_source_id == stats.monitor_source_id)
                .order_by(FetchStats.id.desc())
                .limit(28)
            )
            history = [row[0] for row in history_result.all()]

            health = await health_repo.load(stats.monitor_source_id)
            verdict = evaluate(
                kept=stats.kept,
                previously_had_rows=health.last_success_at is not None,
                consecutive_empty=health.consecutive_empty,
                rows_median_28d=health.rows_median_28d,
                history_count=len(history),
            )

            consecutive_empty = 0 if stats.kept > 0 else health.consecutive_empty + 1
            consecutive_fail = 0 if not stats.error else health.consecutive_fail + 1
            last_success_at = (
                datetime.now(timezone.utc) if stats.kept > 0 else health.last_success_at
            )
            rows_median = statistics.median(history) if len(history) >= 5 else None

            health_repo.save(
                health,
                state=verdict.state,
                consecutive_empty=consecutive_empty,
                consecutive_fail=consecutive_fail,
                last_success_at=last_success_at,
                rows_median_28d=rows_median,
                library_version=stats.library_version,
            )

            if verdict.state in (SourceState.BROKEN, SourceState.DEGRADED):
                kind = (
                    AlertKind.EMPTY_STREAK
                    if verdict.state is SourceState.BROKEN
                    else AlertKind.VOLUME_COLLAPSE
                )
                source = await session.get(MonitorSource, stats.monitor_source_id)
                alert_repo.raise_alert(
                    monitor_id=source.monitor_id,
                    monitor_source_id=stats.monitor_source_id,
                    kind=kind,
                    observed=str(stats.kept),
                    expected=str(health.rows_median_28d),
                )

        await session.commit()
