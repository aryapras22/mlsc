"""Topic coherence, diversity, and week-over-week stability (requirement 3).

Coherence is embedding-based — average pairwise cosine similarity between a
topic's member document embeddings — computed directly rather than through
a topic-modelling coherence library, since it reuses the sentence-
transformers embedder `document-enrichment` already pins and runs
(tasks.md, task 1's decision).
"""

from __future__ import annotations

import uuid

import numpy as np
from sklearn.metrics import adjusted_rand_score
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import Assignment, Document, Enrichment, Snapshot, Topic
from mlsc.evaluation.measures import Measure, computed, unavailable_insufficient_history

_MIN_MEMBERS_FOR_COHERENCE = 2


async def measure_coherence(session: AsyncSession, *, monitor_id: uuid.UUID) -> Measure:
    """Requirement 3: average, across every active topic with at least two
    embedded members, of that topic's own mean pairwise cosine similarity."""
    result = await session.execute(
        select(Assignment.topic_id, Enrichment.embedding)
        .join(Document, Document.id == Assignment.document_id)
        .join(Enrichment, Enrichment.document_id == Assignment.document_id)
        .where(Document.monitor_id == monitor_id, Enrichment.embedding.is_not(None))
    )
    embeddings_by_topic: dict[uuid.UUID, list[list[float]]] = {}
    for topic_id, embedding in result.all():
        embeddings_by_topic.setdefault(topic_id, []).append(embedding)

    topic_scores = [
        _mean_pairwise_similarity(embeddings)
        for embeddings in embeddings_by_topic.values()
        if len(embeddings) >= _MIN_MEMBERS_FOR_COHERENCE
    ]
    if not topic_scores:
        return unavailable_insufficient_history(
            "topic_coherence", sample_size=0, computed_over="0 topics with 2+ embedded members"
        )

    return computed(
        "topic_coherence", float(np.mean(topic_scores)), sample_size=len(topic_scores),
        computed_over=f"{len(topic_scores)} topics",
    )


async def measure_diversity(session: AsyncSession, *, monitor_id: uuid.UUID) -> Measure:
    """Requirement 3: how distinct the registry's topics are from each
    other — mean pairwise cosine distance between topic centroids. Low
    diversity means the registry is over-splitting one theme into several
    topics that all mean the same thing."""
    result = await session.execute(select(Topic.centroid).where(Topic.monitor_id == monitor_id))
    centroids = [row[0] for row in result.all()]
    if len(centroids) < 2:
        return unavailable_insufficient_history(
            "topic_diversity", sample_size=len(centroids), computed_over=f"{len(centroids)} topics"
        )

    similarity_matrix = cosine_similarity(np.array(centroids))
    upper_triangle = similarity_matrix[np.triu_indices(len(centroids), k=1)]
    diversity = float(1.0 - np.mean(upper_triangle))
    return computed("topic_diversity", diversity, sample_size=len(centroids), computed_over=f"{len(centroids)} topics")


async def measure_stability(session: AsyncSession, *, monitor_id: uuid.UUID, lookback: int) -> Measure:
    """Requirement 3: agreement between adjacent snapshots via the
    adjusted Rand index, averaged across every available adjacent pair
    within ``lookback`` (design.md, "Domain shapes": ``Snapshot``)."""
    from mlsc.repositories.evaluation import SnapshotRepository

    pairs = await SnapshotRepository(session).adjacent_pairs(monitor_id, limit=lookback)
    if not pairs:
        return unavailable_insufficient_history(
            "topic_stability", sample_size=0, computed_over="0 adjacent snapshot pairs"
        )

    scores = [_agreement(newer, older) for newer, older in pairs]
    return computed(
        "topic_stability", float(np.mean(scores)), sample_size=len(scores),
        computed_over=f"{len(scores)} adjacent snapshot pairs",
    )


def _mean_pairwise_similarity(embeddings: list[list[float]]) -> float:
    matrix = cosine_similarity(np.array(embeddings))
    upper_triangle = matrix[np.triu_indices(len(embeddings), k=1)]
    return float(np.mean(upper_triangle))


def _agreement(newer: Snapshot, older: Snapshot) -> float:
    """ARI over the documents present in both snapshots — a document that
    only exists in one (arrived or was purged between them) contributes no
    signal about whether topic membership *agreed*."""
    shared_ids = set(newer.assignments) & set(older.assignments)
    if not shared_ids:
        return 0.0
    newer_labels = [newer.assignments[document_id] for document_id in shared_ids]
    older_labels = [older.assignments[document_id] for document_id in shared_ids]
    return float(adjusted_rand_score(older_labels, newer_labels))
