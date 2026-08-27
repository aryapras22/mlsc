"""Theme monitor use cases: job submission, query review, and candidate
acceptance/rejection.

``ThemeJobService`` submits generation and discovery as jobs rather than
running them inline (requirements 1, 4, 8, 9); ``ThemeService`` holds the
user-facing decisions that happen between and after those passes —
requirements 2 and 4.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.application.sources import MonitorSourceService
from mlsc.db.models import CandidateState, DiscoverySurface, SourceName, TargetType, ThemeJobKind
from mlsc.repositories.monitors import MonitorRepository
from mlsc.repositories.themes import (
    EntityCandidateNotFound,
    EntityCandidateRepository,
    ThemeJobRepository,
    ThemeSeedRepository,
)
from mlsc.schemas.sources import MonitorSourceCreateRequest

_DEFAULT_DAILY_QUOTA = 50

_SOURCE_NAME_BY_SURFACE = {
    DiscoverySurface.APP_STORE_SEARCH: SourceName.APPSTORE,
    DiscoverySurface.PLAY_SEARCH: SourceName.PLAY,
    DiscoverySurface.NEWS_QUERY: SourceName.NEWS,
    DiscoverySurface.FEED_DISCOVERY: SourceName.HACKERNEWS,
}


class ReviewedQuery(BaseModel):
    text: str
    rationale: str


class CandidateNotViable(RuntimeError):
    """An accepted candidate could not be attached as a source.

    The candidate is left ``proposed`` rather than being silently discarded
    — the user's decision to accept it is not the thing that was wrong
    (design.md, "Failure strategy").
    """

    def __init__(self, candidate_id: uuid.UUID, reason: str) -> None:
        super().__init__(f"candidate {candidate_id} could not be attached: {reason}")
        self.candidate_id = candidate_id


class NotAThemeMonitor(RuntimeError):
    """Raised when generation or discovery is requested for a monitor whose
    ``target_type`` is not ``THEME`` — a product monitor has no seed to
    generate queries from (requirement 8; design.md, "Failure strategy")."""

    def __init__(self, monitor_id: uuid.UUID) -> None:
        super().__init__(str(monitor_id))
        self.monitor_id = monitor_id


class ThemeJobOverlaps(RuntimeError):
    """Raised when a generation or discovery job of the same kind is already
    in flight for this monitor. Carries the running job's id, matching
    ``OverrideOverlaps``'s shape exactly (requirement 9)."""

    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(str(job_id))
        self.job_id = job_id


class ThemeDispatcher(Protocol):
    """Injected so this service never imports Celery directly (design.md,
    "Dependencies, injected")."""

    def dispatch_theme_job(self, job_id: uuid.UUID) -> None: ...


class ThemeService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._sources = MonitorSourceService(session_factory)

    async def review_queries(
        self, monitor_id: uuid.UUID, queries: list[ReviewedQuery]
    ) -> None:
        """Requirement 2: replace the generated query set with the user's
        edited, reduced set — every one of these is accepted, because
        review is the act of approving exactly this final list for
        discovery to use."""
        async with self._session_factory() as session:
            repository = ThemeSeedRepository(session)
            seed = await repository.get_by_monitor(monitor_id)
            await repository.upsert(
                monitor_id,
                description=seed.description,
                queries=[
                    {"text": query.text, "rationale": query.rationale, "accepted": True}
                    for query in queries
                ],
                provenance=seed.provenance,
            )
            await session.commit()

    async def list_candidates(
        self, monitor_id: uuid.UUID, *, state: CandidateState | None = None
    ) -> list:
        async with self._session_factory() as session:
            return await EntityCandidateRepository(session).list_for_monitor(
                monitor_id, state=state
            )

    async def accept_candidate(self, monitor_id: uuid.UUID, candidate_id: uuid.UUID) -> uuid.UUID:
        """Requirement 4: an accepted candidate becomes an ordinary attached
        source, through the same path a named monitor's sources use — a
        discovered app is indistinguishable from one a user named directly
        once it is attached (design.md, "Success path")."""
        async with self._session_factory() as session:
            candidate = await EntityCandidateRepository(session).get(candidate_id)
            if candidate.monitor_id != monitor_id:
                raise EntityCandidateNotFound(candidate_id)
            source_name = _SOURCE_NAME_BY_SURFACE[DiscoverySurface(candidate.provenance["surface"])]
            config = _config_for(source_name, candidate.entity_ref)

        try:
            source = await self._sources.attach(
                monitor_id,
                MonitorSourceCreateRequest(
                    source_name=source_name, config=config, daily_quota=_DEFAULT_DAILY_QUOTA
                ),
            )
        except ValueError as error:
            raise CandidateNotViable(candidate_id, str(error)) from None

        async with self._session_factory() as session:
            repository = EntityCandidateRepository(session)
            candidate = await repository.get(candidate_id)
            candidate.state = CandidateState.ACCEPTED
            candidate.reviewed_at = datetime.now(timezone.utc)
            await session.commit()
        return source.id

    async def reject_candidate(self, monitor_id: uuid.UUID, candidate_id: uuid.UUID) -> None:
        """Requirement 6: permanent — there is no expiry that would let this
        candidate be proposed again (design.md, "Closed variants")."""
        async with self._session_factory() as session:
            repository = EntityCandidateRepository(session)
            candidate = await repository.get(candidate_id)
            if candidate.monitor_id != monitor_id:
                raise EntityCandidateNotFound(candidate_id)
            candidate.state = CandidateState.REJECTED
            candidate.reviewed_at = datetime.now(timezone.utc)
            await session.commit()


class ThemeJobService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        dispatcher: ThemeDispatcher,
    ) -> None:
        self._session_factory = session_factory
        self._dispatcher = dispatcher

    async def submit_generation(self, monitor_id: uuid.UUID) -> uuid.UUID:
        """Requirement 1: returns an identifier for the work rather than the
        queries themselves — generation calls an LLM, and C12 forbids doing
        that inline (design.md, "Success path")."""
        return await self._submit(monitor_id, ThemeJobKind.QUERY_GENERATION)

    async def submit_discovery(self, monitor_id: uuid.UUID) -> uuid.UUID:
        """Requirement 4: same guard and job shape as generation — discovery
        calls several third-party surfaces, so it is enqueued for the same
        C12 reason (design.md, "Success path")."""
        return await self._submit(monitor_id, ThemeJobKind.DISCOVERY)

    async def _submit(self, monitor_id: uuid.UUID, kind: ThemeJobKind) -> uuid.UUID:
        async with self._session_factory() as session:
            monitor = await MonitorRepository(session).get(monitor_id)
            if monitor.target_type is not TargetType.THEME:
                raise NotAThemeMonitor(monitor_id)

            jobs = ThemeJobRepository(session)
            in_flight = await jobs.find_in_flight(monitor_id, kind)
            if in_flight is not None:
                raise ThemeJobOverlaps(in_flight.id)

            job = await jobs.create_pending(monitor_id, kind)
            await session.commit()
            job_id = job.id

        self._dispatcher.dispatch_theme_job(job_id)
        return job_id


def _config_for(source_name: SourceName, entity_ref: str) -> dict:
    if source_name is SourceName.APPSTORE:
        return {"app_id": entity_ref}
    if source_name is SourceName.PLAY:
        return {"package_id": entity_ref}
    return {"queries": [entity_ref]}
