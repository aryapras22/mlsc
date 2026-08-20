"""Persistence for the topic registry: nearest-centroid search, drift, and
last-seen tracking.

Takes a caller-owned ``AsyncSession`` and never commits, matching
``mlsc/repositories/monitors.py``. Nearest-centroid search is one SQL query
using the HNSW index on ``topics.centroid`` rather than a Python-side scan —
the entire argument in learn.md for keeping the vector in Postgres.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import Lineage, LineageEvent, Topic, TopicStatus


class TopicNotFound(KeyError):
    """Raised when a ``TopicId`` does not resolve to a stored topic."""

    def __init__(self, topic_id: uuid.UUID) -> None:
        super().__init__(str(topic_id))
        self.topic_id = topic_id


class NearestTopic:
    """One nearest-centroid search result: the topic and its cosine similarity."""

    __slots__ = ("topic", "similarity")

    def __init__(self, topic: Topic, similarity: float) -> None:
        self.topic = topic
        self.similarity = similarity


class TopicRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def insert(self, topic: Topic) -> None:
        self._session.add(topic)

    async def get(self, topic_id: uuid.UUID) -> Topic:
        topic = await self._session.get(Topic, topic_id, populate_existing=True)
        if topic is None:
            raise TopicNotFound(topic_id)
        return topic

    async def list_for_monitor(
        self, monitor_id: uuid.UUID, *, statuses: tuple[TopicStatus, ...] | None = None
    ) -> list[Topic]:
        query = select(Topic).where(Topic.monitor_id == monitor_id)
        if statuses is not None:
            query = query.where(Topic.status.in_(statuses))
        result = await self._session.execute(query.order_by(Topic.first_seen))
        return list(result.scalars().all())

    async def nearest_active(
        self, monitor_id: uuid.UUID, embedding: list[float]
    ) -> NearestTopic | None:
        """The active, unmerged topic whose centroid is closest to ``embedding``.

        ``cosine_distance`` is ``1 - cosine_similarity``, so similarity is
        recovered as ``1 - distance`` for the caller to compare against a
        threshold expressed the way every other threshold in this spec is.
        """
        distance = Topic.centroid.cosine_distance(embedding)
        result = await self._session.execute(
            select(Topic, distance)
            .where(Topic.monitor_id == monitor_id, Topic.status == TopicStatus.ACTIVE)
            .order_by(distance)
            .limit(1)
        )
        row = result.first()
        if row is None:
            return None
        topic, topic_distance = row
        return NearestTopic(topic=topic, similarity=1.0 - topic_distance)

    def drift_centroid(
        self, topic: Topic, embedding: list[float], *, drift_factor: float, member_count: int = 1
    ) -> None:
        """Move the centroid a little toward ``embedding`` (an EWMA update) and
        accumulate the movement into ``drift_score`` (requirement 2).

        ``drift_factor`` is deliberately small: see learn.md, "EWMA centroid
        drift, and why the factor is small". ``member_count`` is how many
        documents this one update represents — 1 for a single assignment,
        more when ``embedding`` is a candidate centroid summarising a whole
        merged cluster — so ``doc_count`` stays accurate either way.
        """
        moved = _distance(topic.centroid, embedding) * drift_factor
        topic.centroid = [
            (1 - drift_factor) * current + drift_factor * incoming
            for current, incoming in zip(topic.centroid, embedding, strict=True)
        ]
        topic.drift_score += moved
        topic.doc_count += member_count

    def touch_last_seen(self, topic: Topic, *, today: date) -> None:
        topic.last_seen = today


def _distance(a: list[float], b: list[float]) -> float:
    """Euclidean distance between two embeddings, used only to size the drift
    increment — the registry's own similarity search uses cosine distance in
    SQL, not this helper."""
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) ** 0.5


class LineageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def write(
        self,
        *,
        from_topic: uuid.UUID,
        to_topic: uuid.UUID | None,
        event: LineageEvent,
        reason: str | None = None,
    ) -> Lineage:
        record = Lineage(
            id=uuid.uuid4(), from_topic=from_topic, to_topic=to_topic, event=event, reason=reason
        )
        self._session.add(record)
        return record
