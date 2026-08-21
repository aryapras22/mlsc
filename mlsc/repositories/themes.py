"""Persistence for theme seeds and entity candidates.

Repositories take a caller-owned ``AsyncSession`` and never commit; the
application service owns the transaction boundary, matching
``mlsc/repositories/monitors.py`` and ``mlsc/repositories/sources.py``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import CandidateState, EntityCandidate, SourceName, ThemeSeed


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
