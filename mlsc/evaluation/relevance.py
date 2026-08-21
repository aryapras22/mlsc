"""Relevance precision and recall against a hand-labelled document sample
(requirement 2). Measures the pipeline's own ``Enrichment.is_relevant``
verdict, never the labels used to build it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import DocumentLabel, Enrichment
from mlsc.evaluation.measures import Measure, computed, unavailable_no_labels


async def measure_relevance(
    session: AsyncSession, *, labels: list[DocumentLabel]
) -> tuple[Measure, Measure]:
    """Confusion counts over labelled documents, against the system's own
    relevance verdict for each — returns (precision, recall)."""
    if not labels:
        return (
            unavailable_no_labels("relevance_precision", computed_over="0 labelled documents"),
            unavailable_no_labels("relevance_recall", computed_over="0 labelled documents"),
        )

    document_ids = [label.document_id for label in labels]
    result = await session.execute(
        select(Enrichment.document_id, Enrichment.is_relevant).where(Enrichment.document_id.in_(document_ids))
    )
    predicted_by_document: dict[uuid.UUID, bool] = {
        document_id: bool(is_relevant) for document_id, is_relevant in result.all()
    }

    true_positive = false_positive = false_negative = 0
    for label in labels:
        predicted = predicted_by_document.get(label.document_id, False)
        if predicted and label.is_relevant:
            true_positive += 1
        elif predicted and not label.is_relevant:
            false_positive += 1
        elif not predicted and label.is_relevant:
            false_negative += 1

    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 0.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 0.0

    computed_over = f"{len(labels)} labelled documents"
    return (
        computed("relevance_precision", precision, sample_size=len(labels), computed_over=computed_over),
        computed("relevance_recall", recall, sample_size=len(labels), computed_over=computed_over),
    )
