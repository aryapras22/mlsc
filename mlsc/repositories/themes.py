"""Persistence for theme seeds and entity candidates.

Repositories take a caller-owned ``AsyncSession`` and never commit; the
application service owns the transaction boundary, matching
``mlsc/repositories/monitors.py`` and ``mlsc/repositories/sources.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import (
    CandidateState,
    EntityCandidate,
    SourceName,
    ThemeJob,
    ThemeJobKind,
    ThemeJobStatus,
    ThemeSeed,
)

_IN_FLIGHT = (ThemeJobStatus.PENDING, ThemeJobStatus.RUNNING)


class ThemeSeedNotFound(KeyError):
    """Raised when a monitor has no stored seed yet."""

    def __init__(self, monitor_id: uuid.UUID) -> None:
        super().__init__(str(monitor_id))
        self.monitor_id = monitor_id


class EntityCandidateNotFound(KeyError):
    """Raised when a ``CandidateId`` does not resolve to a stored candidate."""

    def __init__(self, candidate_id: uuid.UUID) -> None:
        super().__init__(str(candidate_id))
        self.candidate_id = candidate_id


class ThemeJobNotFound(KeyError):
    """Raised when a ``job_id`` does not resolve to a stored job."""

    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(str(job_id))
        self.job_id = job_id


class ThemeSeedRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_monitor(self, monitor_id: uuid.UUID) -> ThemeSeed:
        result = await self._session.execute(
            select(ThemeSeed).where(ThemeSeed.monitor_id == monitor_id)
        )
        seed = result.scalar_one_or_none()
        if seed is None:
            raise ThemeSeedNotFound(monitor_id)
        return seed

    async def upsert(
        self, monitor_id: uuid.UUID, *, description: str, queries: list[dict], provenance: dict
    ) -> ThemeSeed:
        """Insert the seed on first use, or overwrite its queries on a later pass.

        The description only ever comes from monitor creation, so it is
        written once here and left alone on every later call — a user
        editing queries must not silently rewrite the intent that produced
        them (design.md, "Domain shapes").
        """
        result = await self._session.execute(
            select(ThemeSeed).where(ThemeSeed.monitor_id == monitor_id)
        )
        seed = result.scalar_one_or_none()
        if seed is None:
            seed = ThemeSeed(
                id=uuid.uuid4(), monitor_id=monitor_id, description=description,
                queries=queries, provenance=provenance,
            )
            self._session.add(seed)
        else:
            seed.queries = queries
            seed.provenance = provenance
        return seed


class EntityCandidateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, candidate_id: uuid.UUID) -> EntityCandidate:
        candidate = await self._session.get(
            EntityCandidate, candidate_id, populate_existing=True
        )
        if candidate is None:
            raise EntityCandidateNotFound(candidate_id)
        return candidate

    async def list_for_monitor(
        self, monitor_id: uuid.UUID, *, state: CandidateState | None = None
    ) -> list[EntityCandidate]:
        query = select(EntityCandidate).where(EntityCandidate.monitor_id == monitor_id)
        if state is not None:
            query = query.where(EntityCandidate.state == state)
        result = await self._session.execute(query.order_by(EntityCandidate.created_at))
        return list(result.scalars().all())

    async def rejected_entity_refs(
        self, monitor_id: uuid.UUID, source_name: SourceName
    ) -> set[str]:
        """Requirement 6: a previously rejected candidate is never reproposed."""
        result = await self._session.execute(
            select(EntityCandidate.entity_ref).where(
                EntityCandidate.monitor_id == monitor_id,
                EntityCandidate.source_name == source_name,
                EntityCandidate.state == CandidateState.REJECTED,
            )
        )
        return set(result.scalars().all())

    async def upsert_proposed(self, candidate: EntityCandidate) -> None:
        """Insert a newly discovered candidate, or leave an existing one as-is.

        A candidate already reviewed (accepted or rejected) must not be
        reset to proposed by a later discovery pass finding it again.
        """
        result = await self._session.execute(
            select(EntityCandidate).where(
                EntityCandidate.monitor_id == candidate.monitor_id,
                EntityCandidate.source_name == candidate.source_name,
                EntityCandidate.entity_ref == candidate.entity_ref,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            self._session.add(candidate)


class ThemeJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, job_id: uuid.UUID) -> ThemeJob:
        job = await self._session.get(ThemeJob, job_id, populate_existing=True)
        if job is None:
            raise ThemeJobNotFound(job_id)
        return job

    async def find_in_flight(
        self, monitor_id: uuid.UUID, kind: ThemeJobKind
    ) -> ThemeJob | None:
        """Requirement 9: a second request for the same monitor and kind is
        rejected while a job is still ``PENDING`` or ``RUNNING``."""
        result = await self._session.execute(
            select(ThemeJob).where(
                ThemeJob.monitor_id == monitor_id,
                ThemeJob.kind == kind,
                ThemeJob.status.in_(_IN_FLIGHT),
            )
        )
        return result.scalar_one_or_none()

    async def create_pending(self, monitor_id: uuid.UUID, kind: ThemeJobKind) -> ThemeJob:
        job = ThemeJob(
            id=uuid.uuid4(), monitor_id=monitor_id, kind=kind, status=ThemeJobStatus.PENDING
        )
        self._session.add(job)
        return job

    async def mark_running(self, job: ThemeJob) -> None:
        job.status = ThemeJobStatus.RUNNING

    async def mark_complete(self, job: ThemeJob) -> None:
        job.status = ThemeJobStatus.COMPLETE
        job.finished_at = datetime.now(timezone.utc)

    async def mark_failed(self, job: ThemeJob, error: str) -> None:
        job.status = ThemeJobStatus.FAILED
        job.finished_at = datetime.now(timezone.utc)
        job.error = error
