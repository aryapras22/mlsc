"""Run lifecycle: start, dispatch, and finalise one monitor's run for one date.

Failures are captured as values, never raised across the fan-out boundary —
one source failing must not abort classification of the others (design.md,
"Failure strategy").
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.core.locks import LockLost, RunLock
from mlsc.db.models import IngestionRun, Monitor, MonitorStatus, MonitorSource, RunStatus

_LOCK_TTL_SECONDS = 900
_STAGE_INGEST = "ingest"


class Stage(str, Enum):
    INGEST = "ingest"


class StageState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class SourceResult(str, Enum):
    """How one source's collection went. Distinguishing these satisfies C5:
    a missing source must never be indistinguishable from a quiet one."""

    COLLECTED = "collected"
    FAILED_VALIDATION = "failed_validation"
    FAILED_TRANSPORT = "failed_transport"
    SKIPPED_DISABLED = "skipped_disabled"
    SKIPPED_BREAKER = "skipped_breaker"


@dataclasses.dataclass(frozen=True)
class SourceOutcome:
    source_id: uuid.UUID
    source_name: str
    result: SourceResult
    kept: int = 0
    error: str | None = None


class MonitorNotRunnable(RuntimeError):
    """Raised when the monitor is paused or archived."""


class RunAlreadyActive(RuntimeError):
    """Raised when a lock is already held for this monitor and date."""

    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(str(run_id))
        self.run_id = run_id


class NoSourcesConfigured(RuntimeError):
    """Raised when the monitor has no enabled source to collect from."""


class AllSourcesEmpty(RuntimeError):
    """Raised when every source produced nothing while at least one was expected to."""


class TaskDispatcher(Protocol):
    """Injected so orchestration never imports Celery directly (design.md,
    "Dependencies, injected")."""

    def dispatch_run(self, run_id: uuid.UUID) -> None: ...


class RunService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        lock: RunLock,
        dispatcher: TaskDispatcher,
    ) -> None:
        self._session_factory = session_factory
        self._lock = lock
        self._dispatcher = dispatcher

    async def start(
        self, monitor_id: uuid.UUID, run_date: date, *, is_backfill: bool = False
    ) -> uuid.UUID:
        async with self._session_factory() as session:
            monitor = await session.get(Monitor, monitor_id)
            if monitor is None or monitor.status is not MonitorStatus.ACTIVE:
                raise MonitorNotRunnable(str(monitor_id))

            has_enabled_source = await session.execute(
                select(MonitorSource.id)
                .where(MonitorSource.monitor_id == monitor_id, MonitorSource.enabled.is_(True))
                .limit(1)
            )
            if has_enabled_source.scalar_one_or_none() is None:
                raise NoSourcesConfigured(str(monitor_id))

            result = await session.execute(
                select(IngestionRun).where(
                    IngestionRun.monitor_id == monitor_id,
                    IngestionRun.run_date == run_date,
                    IngestionRun.is_backfill == is_backfill,
                )
            )
            run = result.scalar_one_or_none()
            if run is None:
                run = IngestionRun(
                    id=uuid.uuid4(),
                    monitor_id=monitor_id,
                    run_date=run_date,
                    is_backfill=is_backfill,
                    stage_status={_STAGE_INGEST: StageState.PENDING.value},
                )
                session.add(run)
                await session.commit()

            if run.status in (RunStatus.COMPLETE, RunStatus.PARTIAL, RunStatus.FAILED):
                return run.id

        lock_key = f"{monitor_id}:{run_date}:{is_backfill}"
        token = await self._lock.acquire(lock_key, ttl_seconds=_LOCK_TTL_SECONDS)
        if token is None:
            raise RunAlreadyActive(run.id)

        self._dispatcher.dispatch_run(run.id)
        return run.id

    async def finalise(
        self, run_id: uuid.UUID, outcomes: list[SourceOutcome], *, expected_volume: bool
    ) -> RunStatus:
        """Classify outcomes and write the final run state in one transaction.

        Runs even when every outcome failed — finalisation is not conditional
        on success, because a run without a final status is worse than a
        failed one (design.md, "Failure strategy").
        """
        collected_total = sum(o.kept for o in outcomes)
        any_collected = any(o.result is SourceResult.COLLECTED for o in outcomes)
        any_failed = any(
            o.result in (SourceResult.FAILED_VALIDATION, SourceResult.FAILED_TRANSPORT)
            for o in outcomes
        )

        if expected_volume and collected_total == 0:
            status = RunStatus.FAILED
        elif any_failed and any_collected:
            status = RunStatus.PARTIAL
        elif any_failed and not any_collected:
            status = RunStatus.FAILED
        else:
            status = RunStatus.COMPLETE

        stage_state = StageState.FAILED if status is RunStatus.FAILED else StageState.COMPLETE

        async with self._session_factory() as session:
            run = await session.get(IngestionRun, run_id)
            if run is None:
                raise ValueError(f"run {run_id} not found")
            run.status = status
            run.stage_status = {**run.stage_status, _STAGE_INGEST: stage_state.value}
            run.finished_at = datetime.now(timezone.utc)
            await session.commit()

        monitor_id, run_date, is_backfill = await self._run_identity(run_id)
        lock_key = f"{monitor_id}:{run_date}:{is_backfill}"
        current_token = await self._lock.current_token(lock_key)
        if current_token is not None:
            try:
                await self._lock.release(lock_key, current_token)
            except LockLost:
                pass

        return status

    async def _run_identity(self, run_id: uuid.UUID) -> tuple[uuid.UUID, date, bool]:
        async with self._session_factory() as session:
            run = await session.get(IngestionRun, run_id)
            assert run is not None
            return run.monitor_id, run.run_date, run.is_backfill

    async def enabled_sources(self, monitor_id: uuid.UUID) -> list[MonitorSource]:
        async with self._session_factory() as session:
            result = await session.execute(
                select(MonitorSource).where(
                    MonitorSource.monitor_id == monitor_id, MonitorSource.enabled.is_(True)
                )
            )
            return list(result.scalars().all())
