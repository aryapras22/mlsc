"""Representative document selection: centroid-nearest with a spread across
sources, kept from the prior codebase's approach (learn.md, "Representative
document").

Nearest-to-centroid alone can return five documents all from the same
source saying the same thing. Spreading across sources gives the model
(and a user reading the evidence links) more than one vantage point on the
same topic within a small document budget.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime

from sqlalchemy import select as sql_select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import Assignment, Document, Enrichment, SourceName, Topic


@dataclasses.dataclass(frozen=True)
class Representative:
    document_id: uuid.UUID
    excerpt: str
    source: SourceName
    published_at: datetime
    rating: int | None


async def select(
    session: AsyncSession, *, topic: Topic, period_start: date, period_end: date, limit: int
) -> list[Representative]:
    """The ``limit`` nearest-to-centroid documents for this topic and period,
    spread across sources: one pass per source in round-robin order of
    cosine distance, so no single source can fill the whole budget while
    others are unrepresented.
    """
    distance = Enrichment.embedding.cosine_distance(topic.centroid)
    result = await session.execute(
        sql_select(Document, distance)
        .join(Enrichment, Enrichment.document_id == Document.id)
        .join(Assignment, Assignment.document_id == Document.id)
        .where(
            Assignment.topic_id == topic.id,
            Document.published_at >= period_start,
            Document.published_at < period_end,
            Enrichment.is_relevant.is_(True),
            Enrichment.embedding.is_not(None),
            Document.body.is_not(None),
        )
        .order_by(distance)
    )
    ranked = [document for document, _distance in result.all()]

    by_source: dict[SourceName, list[Document]] = {}
    for document in ranked:
        by_source.setdefault(document.source_name, []).append(document)

    selected: list[Document] = []
    while len(selected) < limit and any(by_source.values()):
        for source_documents in by_source.values():
            if not source_documents:
                continue
            selected.append(source_documents.pop(0))
            if len(selected) >= limit:
                break

    return [
        Representative(
            document_id=document.id, excerpt=document.body, source=document.source_name,
            published_at=document.published_at, rating=document.rating,
        )
        for document in selected
    ]
