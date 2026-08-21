"""Requirement 9's use case: recording a usefulness verdict against an
existing insight.

Untrusted user feedback is validated here, at the application boundary, as a
reference to an existing insight (design.md, "Trust boundary") — not inside
the repository, and not left to the database's foreign key to discover.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.db.models import Judgement
from mlsc.repositories.insights import InsightRepository


class InsightNotFound(KeyError):
    """Raised when an ``InsightId`` does not resolve to a stored insight."""

    def __init__(self, insight_id: uuid.UUID) -> None:
        super().__init__(str(insight_id))
        self.insight_id = insight_id


class JudgementService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, insight_id: uuid.UUID, *, useful: bool) -> uuid.UUID:
        async with self._session_factory() as session:
            insight = await InsightRepository(session).get(insight_id)
            if insight is None:
                raise InsightNotFound(insight_id)

            judgement_id = uuid.uuid4()
            session.add(Judgement(id=judgement_id, insight_id=insight_id, useful=useful))
            await session.commit()
            return judgement_id
