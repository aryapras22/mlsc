"""Evidence selection: the documents behind one candidate, so a user can
interrogate a change rather than take it on faith (requirement 1, C4).

Refuses to select nothing — an event without evidence is a claim with no
way to check it, which is exactly what design.md's failure strategy crashes
on ("Evidence selection returning nothing — crash before the event is
written").
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select as sql_select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import Assignment, DetectionMethod, Document, Enrichment

_MAX_EVIDENCE_DOCUMENTS = 5


class NoEvidenceAvailable(RuntimeError):
    """A candidate's bucket has no documents to point to. Raised so the
    caller can crash before writing the event, per design.md's failure
    strategy — never silently write an event with an empty evidence list."""


async def select(
    session: AsyncSession, *, topic_id: uuid.UUID, bucket: date, method: DetectionMethod
) -> list[str]:
    """The document ids that best explain this candidate.

    A window-based method (Mann-Kendall, changepoint) looks across its
    trailing days for evidence since no single day defines a trend; a
    point method (robust z, Poisson, Kleinberg) looks only at the day
    itself, where the anomaly actually occurred.
    """
    window_days = 7 if method in (DetectionMethod.MANN_KENDALL, DetectionMethod.PELT) else 1
    window_start = bucket - timedelta(days=window_days - 1)
    next_day = bucket + timedelta(days=1)

    result = await session.execute(
        sql_select(Document.id, Document.published_at, Enrichment.sentiment_score)
        .join(Enrichment, Enrichment.document_id == Document.id)
        .join(Assignment, Assignment.document_id == Document.id)
        .where(
            Assignment.topic_id == topic_id,
            Document.published_at >= window_start,
            Document.published_at < next_day,
            Enrichment.is_relevant.is_(True),
        )
    )
    rows = result.all()
    if not rows:
        raise NoEvidenceAvailable(f"no documents for topic {topic_id} in the window ending {bucket}")

    ranked = sorted(rows, key=lambda row: _rank_key(row, method), reverse=True)
    return [str(document_id) for document_id, _published_at, _sentiment in ranked[:_MAX_EVIDENCE_DOCUMENTS]]


def _rank_key(row: tuple, method: DetectionMethod) -> tuple:
    _document_id, published_at, sentiment_score = row
    if method == DetectionMethod.CTFIDF_DELTA:
        # Sentiment flip and novelty are about what changed in the text
        # itself; a strong sentiment magnitude is the most legible evidence.
        magnitude = abs(sentiment_score) if sentiment_score is not None else 0.0
        return (magnitude, published_at)
    return (published_at,)
