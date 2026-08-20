"""The periodic refit: re-cluster a trailing window and decide whether the
result agrees with history closely enough to apply.

Two measures, two purposes, easy to conflate. The Hungarian algorithm
(``scipy.optimize.linear_sum_assignment``) finds the best one-to-one pairing
between the proposed clusters and the existing registry — it always finds
*a* pairing. The Adjusted Rand Index then asks whether that pairing, once
made, actually agrees with the assignments already on file. A gate on the
second measure, not the first, is what keeps a plausible-looking refit from
silently rewriting history (learn.md, "The refit: Hungarian matching, gated
by ARI").
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
from scipy.optimize import linear_sum_assignment
from sklearn.metrics import adjusted_rand_score
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.config import TopicThresholds
from mlsc.db.models import Assignment, AssignmentMethod, Enrichment, LineageEvent, Topic
from mlsc.pipeline.topics.discovery import Clusterer
from mlsc.repositories.topics import LineageRepository, TopicRepository


@dataclasses.dataclass(frozen=True)
class RefitOutcome:
    applied: bool
    agreement: float
    mapping: dict[int, uuid.UUID]
    reason: str | None = None


@dataclasses.dataclass(frozen=True)
class _WindowMember:
    document_id: uuid.UUID
    topic_id: uuid.UUID
    embedding: list[float]


async def refit_registry(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    thresholds: TopicThresholds,
    clusterer: Clusterer,
    today: date | None = None,
) -> RefitOutcome:
    """Requirement 7: apply the proposed remap only if it agrees closely with
    the current assignment; otherwise record why it did not and change
    nothing.

    Only ``centroid`` and ``clustered`` assignments enter the trailing
    window. A manually assigned document is excluded from both the input and
    any resulting remap — requirement 9's guarantee that automation never
    revisits a manual call holds here because the document simply never
    reaches the comparison, not because of a check that could be forgotten
    downstream.
    """
    today = today or date.today()
    window_start = today - timedelta(days=thresholds.refit_window_days)

    async with session_factory() as session:
        members = await _load_window(session, monitor_id, window_start)
        if len(members) < thresholds.min_cluster_size:
            return RefitOutcome(applied=False, agreement=0.0, mapping={}, reason="window_too_small")

        current_topic_ids = sorted({member.topic_id for member in members})
        topics = TopicRepository(session)
        current_topics = [await topics.get(topic_id) for topic_id in current_topic_ids]

        proposed_labels = clusterer.cluster([member.embedding for member in members])
        mapping, agreement = _match_and_score(members, current_topic_ids, current_topics, proposed_labels)

        if agreement < thresholds.refit_agreement_threshold:
            return RefitOutcome(
                applied=False, agreement=agreement, mapping={}, reason="agreement_below_threshold"
            )

        lineage = LineageRepository(session)
        moved_from: set[uuid.UUID] = set()
        for member, label in zip(members, proposed_labels, strict=True):
            matched_topic_id = mapping.get(label)
            if matched_topic_id is None or matched_topic_id == member.topic_id:
                continue
            await session.execute(
                Assignment.__table__.update()
                .where(Assignment.document_id == member.document_id)
                .values(topic_id=matched_topic_id)
            )
            moved_from.add(member.topic_id)

        for from_topic_id in moved_from:
            lineage.write(from_topic=from_topic_id, to_topic=None, event=LineageEvent.REFIT_REMAP)

        await session.commit()

    return RefitOutcome(applied=True, agreement=agreement, mapping=mapping)


async def _load_window(
    session: AsyncSession, monitor_id: uuid.UUID, window_start: date
) -> list[_WindowMember]:
    result = await session.execute(
        select(Assignment.document_id, Assignment.topic_id, Enrichment.embedding)
        .join(Enrichment, Enrichment.document_id == Assignment.document_id)
        .join(Topic, Topic.id == Assignment.topic_id)
        .where(
            Topic.monitor_id == monitor_id,
            Assignment.method != AssignmentMethod.MANUAL,
            Assignment.assigned_at >= datetime.combine(window_start, time.min, tzinfo=timezone.utc),
        )
    )
    return [
        _WindowMember(document_id=doc_id, topic_id=topic_id, embedding=embedding)
        for doc_id, topic_id, embedding in result.all()
    ]


def _match_and_score(
    members: list[_WindowMember],
    current_topic_ids: list[uuid.UUID],
    current_topics: list[Topic],
    proposed_labels: list[int],
) -> tuple[dict[int, uuid.UUID], float]:
    """Hungarian-match proposed clusters to registry topics by centroid
    cosine similarity, then score the resulting pairing against the current
    assignment with the Adjusted Rand Index."""
    proposed_cluster_ids = sorted({label for label in proposed_labels if label != -1})
    if not proposed_cluster_ids:
        return {}, 0.0

    proposed_centroids = [
        _mean_embedding(
            [m.embedding for m, label in zip(members, proposed_labels, strict=True) if label == cluster_id]
        )
        for cluster_id in proposed_cluster_ids
    ]
    topic_centroids = [topic.centroid for topic in current_topics]

    similarity = _cosine_similarity_matrix(proposed_centroids, topic_centroids)
    row_indices, col_indices = linear_sum_assignment(-similarity)

    mapping = {
        proposed_cluster_ids[row]: current_topic_ids[col]
        for row, col in zip(row_indices, col_indices, strict=True)
    }

    current_labels = [str(member.topic_id) for member in members]
    proposed_topic_labels = [
        str(mapping.get(label, f"unmatched:{label}")) for label in proposed_labels
    ]
    agreement = adjusted_rand_score(current_labels, proposed_topic_labels)
    return mapping, agreement


def _mean_embedding(vectors: list[list[float]]) -> list[float]:
    array = np.asarray(vectors)
    return array.mean(axis=0).tolist()


def _cosine_similarity_matrix(a: list[list[float]], b: list[list[float]]) -> np.ndarray:
    a_array = np.asarray(a)
    b_array = np.asarray(b)
    a_norm = a_array / np.linalg.norm(a_array, axis=1, keepdims=True)
    b_norm = b_array / np.linalg.norm(b_array, axis=1, keepdims=True)
    return a_norm @ b_norm.T
