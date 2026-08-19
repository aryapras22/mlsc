"""Conflict-ignoring document insertion.

Uniqueness on (monitor_id, source_name, external_id) is enforced by the
database, not by an existence check first — a check-then-insert has a race
window between two workers collecting the same source (learn.md, "Idempotency
by natural key").
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import Document


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_ignoring_duplicates(self, rows: list[dict[str, Any]]) -> int:
        """Insert every row, skipping ones that collide on the natural key.

        Returns the number of rows actually written; ``len(rows) - written`` is
        the duplicate count the stats ledger records.
        """
        if not rows:
            return 0
        statement = (
            insert(Document)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_documents_natural_key")
            .returning(Document.id)
        )
        result = await self._session.execute(statement)
        return len(result.fetchall())

    async def get(self, document_id: uuid.UUID) -> Document | None:
        return await self._session.get(Document, document_id)
