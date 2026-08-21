"""The seam a caller's identity resolves through before touching a repository.

Single-user for this pass: every monitor is visible, and the seam's only
job today is making ``MonitorNotFound`` the one shape a caller gets back
regardless of cause, so a client cannot learn which monitor identifiers are
real by comparing error shapes (design.md, "Trust boundary"). Multi-tenant
resolution by owner attaches here later as one change, not a rewrite
(learn.md, "Not-found must not leak existence").
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import Monitor
from mlsc.repositories.monitors import MonitorRepository


class Scoping:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, monitor_id: uuid.UUID) -> Monitor:
        """Raises ``MonitorNotFound`` if the id does not resolve — single-user
        admits every existing monitor, so this is presently a not-found
        check rather than an ownership check."""
        return await MonitorRepository(self._session).get(monitor_id)
