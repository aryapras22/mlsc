"""The residue pool: documents that matched no active topic closely enough.

There is no residue table. A document is in the pool precisely when it is
relevant, embedded, and has no ``Assignment`` row yet — so "push" is simply
not writing an assignment, and "clear" is writing one once a candidate
resolves. The pool is a query, not a store, which is what keeps a member from
ever being counted twice (design.md, "Success path": ``ResiduePool.push`` /
``ResiduePool.clear``).
"""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import Assignment, Document, Enrichment


@dataclasses.dataclass(frozen=True)
class ResidueMember:
    document_id: uuid.UUID
    embedding: list[float]


class ResiduePool:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, monitor_id: uuid.UUID) -> list[ResidueMember]:
        """Every relevant, embedded document for this monitor with no assignment."""
        result = await self._session.execute(self._unassigned_query(monitor_id))
        return [
            ResidueMember(document_id=document_id, embedding=embedding)
            for document_id, embedding in result.all()
        ]

    async def size(self, monitor_id: uuid.UUID) -> int:
        """How many documents are waiting on a topic. Growth here means
        assignment is failing (requirement 1's residue-size report)."""
        query = self._unassigned_query(monitor_id).with_only_columns(func.count())
        result = await self._session.execute(query)
        return result.scalar_one()

    def _unassigned_query(self, monitor_id: uuid.UUID):
        return (
            select(Document.id, Enrichment.embedding)
            .join(Enrichment, Enrichment.document_id == Document.id)
            .outerjoin(Assignment, Assignment.document_id == Document.id)
            .where(
                Document.monitor_id == monitor_id,
                Enrichment.is_relevant.is_(True),
                Enrichment.embedding.is_not(None),
                Assignment.id.is_(None),
            )
        )
