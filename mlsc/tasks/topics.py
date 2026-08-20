"""Daily topic assignment and weekly candidate discovery.

Assignment is nearest-centroid lookup for every document without one, holding
anything too dissimilar in the residue pool rather than forcing it
(requirement 1, 2). A document with a manual assignment already carries an
``Assignment`` row, so it never appears in the unassigned selection below —
the override check requirement 9 asks for is structural here, not a
condition to remember. It is an explicit check in ``refit_registry`` instead,
where an existing assignment *is* in scope and could otherwise be overwritten.

Discovery clusters the residue and resolves each cluster into either more
members of an existing topic or a brand-new one (requirement 3, 4).
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.config import TopicThresholds
from mlsc.db.models import Assignment, AssignmentMethod, Document, Topic, TopicStatus
from mlsc.llm.router import LlmRouter
from mlsc.pipeline.topics.discovery import (
    Candidate,
    Clusterer,
    HdbscanClusterer,
    Reducer,
    discover_candidates,
)
from mlsc.pipeline.topics.labeling import LabelUnavailable, extract_keywords, generate_label
from mlsc.pipeline.topics.registry import ResiduePool
from mlsc.repositories.topics import TopicRepository


@dataclasses.dataclass(frozen=True)
class AssignmentOutcome:
    assigned: int
    residue_size: int


async def assign_topics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    thresholds: TopicThresholds,
    today: date | None = None,
) -> AssignmentOutcome:
    today = today or date.today()
    assigned = 0

    async with session_factory() as session:
        pool = ResiduePool(session)
        topics = TopicRepository(session)
        members = await pool.load(monitor_id)

        for member in members:
            nearest = await topics.nearest_active(monitor_id, member.embedding)
            if nearest is None or nearest.similarity < thresholds.assignment_threshold:
                continue

            session.add(
                Assignment(
                    id=uuid.uuid4(),
                    document_id=member.document_id,
                    topic_id=nearest.topic.id,
                    similarity=nearest.similarity,
                    method=AssignmentMethod.CENTROID,
                )
            )
            topics.drift_centroid(
                nearest.topic, member.embedding, drift_factor=thresholds.drift_factor
            )
            topics.touch_last_seen(nearest.topic, today=today)
            assigned += 1

        await session.commit()

        residue_size = await pool.size(monitor_id)

    return AssignmentOutcome(assigned=assigned, residue_size=residue_size)


async def mark_dormant_topics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    thresholds: TopicThresholds,
    today: date | None = None,
) -> int:
    """Mark long-silent active topics dormant. Never deletes a row
    (requirement 6) — dormant is a status, not an absence."""
    today = today or date.today()
    cutoff = today - timedelta(days=thresholds.dormancy_days)

    async with session_factory() as session:
        topics = TopicRepository(session)
        active = await topics.list_for_monitor(monitor_id, statuses=(TopicStatus.ACTIVE,))
        silent = [topic for topic in active if topic.last_seen < cutoff]
        for topic in silent:
            topic.status = TopicStatus.DORMANT
        await session.commit()

    return len(silent)


@dataclasses.dataclass(frozen=True)
class DiscoveryOutcome:
    """``emerged_topic_ids`` is the emergence event requirement 4 asks for.

    There is no event table yet — gating what happens with an emergence is
    `trend-detection`'s job (requirements.md, "Deferred") — so the emergence
    is surfaced here as a value the caller can act on, rather than invented
    infrastructure this spec does not need.
    """

    merged: int
    created: int
    emerged_topic_ids: list[uuid.UUID]


async def discover_topics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    thresholds: TopicThresholds,
    llm_router: LlmRouter | None,
    reducer: Reducer,
    clusterer: Clusterer | None = None,
    today: date | None = None,
) -> DiscoveryOutcome:
    """Cluster the residue pool and resolve each cluster (requirement 3, 4).

    Guards the pool size before clustering, per requirement 1: a pool below
    ``min_residue_pool_size`` is left alone rather than forced through
    discovery early, since a handful of documents cannot honestly form a
    cluster with anything.
    """
    today = today or date.today()
    clusterer = clusterer or HdbscanClusterer(min_cluster_size=thresholds.min_cluster_size)

    async with session_factory() as session:
        pool = ResiduePool(session)
        members = await pool.load(monitor_id)
        if len(members) < thresholds.min_residue_pool_size:
            return DiscoveryOutcome(merged=0, created=0, emerged_topic_ids=[])

        candidates = discover_candidates(members, reducer=reducer, clusterer=clusterer)
        pseudo_documents = [
            await _pseudo_document(session, candidate) for candidate in candidates
        ]

        topics = TopicRepository(session)
        merged = 0
        created = 0
        emerged_topic_ids: list[uuid.UUID] = []

        for index, candidate in enumerate(candidates):
            nearest = await topics.nearest_active(monitor_id, candidate.centroid)
            if (
                nearest is not None
                and nearest.similarity >= thresholds.merge_threshold
                and not nearest.topic.is_pinned
            ):
                # No lineage row here: a candidate never had an identifier of
                # its own, so there is no earlier identity that needs to stay
                # resolvable. Lineage records the history of a topic that
                # already existed — the manual merge in application/topics.py
                # and the refit remap are what write it (requirement 5).
                _merge_candidate_into(session, candidate, nearest.topic, thresholds=thresholds, today=today)
                merged += 1
            else:
                new_topic = await _create_topic_from_candidate(
                    candidate,
                    monitor_id,
                    pseudo_documents=pseudo_documents,
                    index=index,
                    llm_router=llm_router,
                    today=today,
                )
                topics.insert(new_topic)
                await session.flush()
                _assign_members(session, candidate, new_topic, today=today)
                created += 1
                emerged_topic_ids.append(new_topic.id)

        await session.commit()

    return DiscoveryOutcome(merged=merged, created=created, emerged_topic_ids=emerged_topic_ids)


async def _pseudo_document(session: AsyncSession, candidate: Candidate) -> str:
    """Concatenate a candidate's member texts into one pseudo-document, the
    c-TF-IDF unit of comparison (learn.md, "c-TF-IDF names a cluster")."""
    result = await session.execute(
        select(Document.body).where(Document.id.in_(candidate.member_document_ids))
    )
    return " ".join(text for (text,) in result.all() if text)


def _merge_candidate_into(
    session: AsyncSession,
    candidate: Candidate,
    topic: Topic,
    *,
    thresholds: TopicThresholds,
    today: date,
) -> None:
    for document_id in candidate.member_document_ids:
        session.add(
            Assignment(
                id=uuid.uuid4(),
                document_id=document_id,
                topic_id=topic.id,
                similarity=thresholds.merge_threshold,
                method=AssignmentMethod.CLUSTERED,
            )
        )
    # One drift update using the candidate's own centroid, rather than one
    # per member: the candidate centroid already summarises every member,
    # and updating once keeps drift proportional to how different the new
    # material is, not to how many documents happened to arrive.
    TopicRepository(session).drift_centroid(
        topic,
        candidate.centroid,
        drift_factor=thresholds.drift_factor,
        member_count=len(candidate.member_document_ids),
    )
    topic.last_seen = today


def _assign_members(session: AsyncSession, candidate: Candidate, topic: Topic, *, today: date) -> None:
    for document_id in candidate.member_document_ids:
        session.add(
            Assignment(
                id=uuid.uuid4(),
                document_id=document_id,
                topic_id=topic.id,
                similarity=1.0,
                method=AssignmentMethod.CLUSTERED,
            )
        )
    topic.doc_count = len(candidate.member_document_ids)
    topic.last_seen = today


async def _create_topic_from_candidate(
    candidate: Candidate,
    monitor_id: uuid.UUID,
    *,
    pseudo_documents: list[str],
    index: int,
    llm_router: LlmRouter | None,
    today: date,
) -> Topic:
    keywords = extract_keywords(pseudo_documents, index=index) if pseudo_documents[index] else []

    label = " ".join(keywords[:3]) or "unlabelled topic"
    provisional = True
    provider = model = prompt_version = None

    if llm_router is not None and keywords:
        try:
            completion = await generate_label(
                llm_router, keywords=keywords, example_texts=[pseudo_documents[index]]
            )
        except LabelUnavailable:
            pass
        else:
            label = completion.value.label
            provisional = False
            provider, model, prompt_version = (
                completion.provider,
                completion.model,
                completion.prompt_version,
            )

    return Topic(
        id=uuid.uuid4(),
        monitor_id=monitor_id,
        label=label,
        keywords=keywords,
        centroid=candidate.centroid,
        doc_count=0,
        first_seen=today,
        last_seen=today,
        status=TopicStatus.ACTIVE,
        label_is_provisional=provisional,
        label_provider=provider,
        label_model=model,
        label_prompt_version=prompt_version,
    )
