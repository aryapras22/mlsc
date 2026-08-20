"""Core tests for the persistent topic registry. No stochastic algorithm in
the loop: the reducer and clusterer are deterministic stubs over fixed
vectors (design.md, "Dependencies, injected").

Requirements: 1, 2, 5, 6, 7, 8, 9.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import pool, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.application.topics import MergeTargetInactive, TopicService
from mlsc.config import TopicThresholds
from mlsc.db.models import (
    Assignment,
    AssignmentMethod,
    Base,
    Document,
    Enrichment,
    Lineage,
    SourceName,
    SplitProposal,
    SplitProposalStatus,
    TargetType,
    Topic,
    TopicStatus,
)
from mlsc.pipeline.normalize import hash_author, hash_content
from mlsc.pipeline.topics.refit import refit_registry
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.tasks.topics import assign_topics, discover_topics, mark_dormant_topics

LOCAL_DATABASE_URL = "postgresql+asyncpg://mlsc:mlsc@localhost:55433/mlsc"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _reachable(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except (OperationalError, OSError):
        return False


async def _reset_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


@pytest.fixture
def session_factory() -> async_sessionmaker:
    engine = create_async_engine(LOCAL_DATABASE_URL, poolclass=pool.NullPool)
    if not run(_reachable(engine)):
        run(engine.dispose())
        pytest.skip("local Compose PostgreSQL is not reachable at localhost:55433")
    run(_reset_schema(engine))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    run(engine.dispose())


class FixedReducer:
    """Returns the embedding's first two dimensions unchanged: no stochastic
    projection, so a test can assert on cluster membership directly."""

    def reduce(self, embeddings: list[list[float]]) -> list[list[float]]:
        return [vector[:2] for vector in embeddings]


class FixedClusterer:
    """Splits by which of the first two dimensions is larger. Deterministic,
    so discovery and refit tests never depend on a stochastic algorithm."""

    def cluster(self, reduced: list[list[float]]) -> list[int]:
        return [0 if vector[0] >= vector[1] else 1 for vector in reduced]


async def _make_monitor(session_factory: async_sessionmaker) -> uuid.UUID:
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Roblox", target_type=TargetType.PRODUCT, seed={"identifiers": ["x"]},
            cron_expression="0 3 * * *", timezone="UTC", retention_days=90,
        )
    )
    return monitor.id


async def _add_document_with_embedding(
    session_factory: async_sessionmaker, *, monitor_id: uuid.UUID, embedding: list[float]
) -> uuid.UUID:
    document_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Document(
                id=document_id, monitor_id=monitor_id, source_name=SourceName.PLAY,
                external_id=str(document_id), entity_id="x", url=None,
                author_hash=hash_author("u"), body="text", published_at=datetime.now(timezone.utc),
                rating=5, app_version="1", engagement=None,
                content_hash=hash_content(str(document_id)), raw={},
            )
        )
        await session.flush()
        session.add(
            Enrichment(
                id=uuid.uuid4(), document_id=document_id, is_relevant=True,
                embedding=embedding, model_versions={},
            )
        )
        await session.commit()
    return document_id


async def _add_topic(
    session_factory: async_sessionmaker,
    *,
    monitor_id: uuid.UUID,
    centroid: list[float],
    label: str = "topic",
    last_seen: date | None = None,
    is_pinned: bool = False,
) -> uuid.UUID:
    topic_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Topic(
                id=topic_id, monitor_id=monitor_id, label=label, keywords=[], centroid=centroid,
                doc_count=0, first_seen=date.today(), last_seen=last_seen or date.today(),
                is_pinned=is_pinned,
            )
        )
        await session.commit()
    return topic_id


def _far_vector(base: list[float]) -> list[float]:
    return base + [0.0] * (384 - len(base))


class TestAssignment:
    def test_a_similar_document_is_assigned_and_an_identifier_survives(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_id = run(_add_topic(session_factory, monitor_id=monitor_id, centroid=_far_vector([1.0, 0.0])))
        document_id = run(
            _add_document_with_embedding(
                session_factory, monitor_id=monitor_id, embedding=_far_vector([0.95, 0.05])
            )
        )

        thresholds = TopicThresholds(assignment_threshold=0.5, merge_threshold=0.75)
        outcome = run(assign_topics(session_factory, monitor_id=monitor_id, thresholds=thresholds))

        assert outcome.assigned == 1
        assert outcome.residue_size == 0

        async def load() -> Assignment:
            async with session_factory() as session:
                result = await session.execute(
                    select(Assignment).where(Assignment.document_id == document_id)
                )
                return result.scalar_one()

        assignment = run(load())
        assert assignment.topic_id == topic_id

    def test_a_dissimilar_document_stays_in_residue_rather_than_being_forced(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        run(_add_topic(session_factory, monitor_id=monitor_id, centroid=_far_vector([1.0, 0.0])))
        run(
            _add_document_with_embedding(
                session_factory, monitor_id=monitor_id, embedding=_far_vector([0.0, 1.0])
            )
        )

        thresholds = TopicThresholds(assignment_threshold=0.9, merge_threshold=0.95)
        outcome = run(assign_topics(session_factory, monitor_id=monitor_id, thresholds=thresholds))

        assert outcome.assigned == 0
        assert outcome.residue_size == 1


class TestDiscovery:
    def test_residue_below_the_pool_threshold_is_left_alone(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        run(
            _add_document_with_embedding(
                session_factory, monitor_id=monitor_id, embedding=_far_vector([1.0, 0.0])
            )
        )

        thresholds = TopicThresholds(min_residue_pool_size=10, min_cluster_size=2)
        outcome = run(
            discover_topics(
                session_factory, monitor_id=monitor_id, thresholds=thresholds, llm_router=None,
                reducer=FixedReducer(),
            )
        )

        assert outcome.created == 0
        assert outcome.merged == 0
        assert outcome.emerged_topic_ids == []

    def test_a_merge_leaves_old_metrics_queryable(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        existing_topic_id = run(
            _add_topic(session_factory, monitor_id=monitor_id, centroid=_far_vector([1.0, 0.0]))
        )
        for _ in range(6):
            run(
                _add_document_with_embedding(
                    session_factory, monitor_id=monitor_id, embedding=_far_vector([0.9, 0.1])
                )
            )

        thresholds = TopicThresholds(
            assignment_threshold=0.1, merge_threshold=0.5, min_residue_pool_size=5, min_cluster_size=2
        )
        outcome = run(
            discover_topics(
                session_factory, monitor_id=monitor_id, thresholds=thresholds, llm_router=None,
                reducer=FixedReducer(), clusterer=FixedClusterer(),
            )
        )

        assert outcome.merged >= 1
        assert outcome.created == 0

        async def topic_still_resolvable() -> Topic:
            async with session_factory() as session:
                return await session.get(Topic, existing_topic_id)

        topic = run(topic_still_resolvable())
        assert topic is not None
        assert topic.status is TopicStatus.ACTIVE

    def test_a_pinned_topic_is_skipped_by_a_matching_candidate(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        run(
            _add_topic(
                session_factory, monitor_id=monitor_id, centroid=_far_vector([1.0, 0.0]),
                is_pinned=True,
            )
        )
        for _ in range(6):
            run(
                _add_document_with_embedding(
                    session_factory, monitor_id=monitor_id, embedding=_far_vector([0.9, 0.1])
                )
            )

        thresholds = TopicThresholds(
            assignment_threshold=0.1, merge_threshold=0.5, min_residue_pool_size=5, min_cluster_size=2
        )
        outcome = run(
            discover_topics(
                session_factory, monitor_id=monitor_id, thresholds=thresholds, llm_router=None,
                reducer=FixedReducer(), clusterer=FixedClusterer(),
            )
        )

        assert outcome.merged == 0
        assert outcome.created >= 1


class TestDormancy:
    def test_a_long_silent_topic_is_marked_dormant_never_deleted(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_id = run(
            _add_topic(
                session_factory, monitor_id=monitor_id, centroid=_far_vector([1.0, 0.0]),
                last_seen=date.today() - timedelta(days=90),
            )
        )

        thresholds = TopicThresholds(dormancy_days=60)
        count = run(mark_dormant_topics(session_factory, monitor_id=monitor_id, thresholds=thresholds))

        assert count == 1

        async def load() -> Topic:
            async with session_factory() as session:
                return await session.get(Topic, topic_id)

        topic = run(load())
        assert topic is not None
        assert topic.status is TopicStatus.DORMANT


class TestManualAssignment:
    def test_a_manual_assignment_is_never_reassigned_by_daily_assignment(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        far_topic_id = run(
            _add_topic(session_factory, monitor_id=monitor_id, centroid=_far_vector([1.0, 0.0]))
        )
        close_topic_id = run(
            _add_topic(session_factory, monitor_id=monitor_id, centroid=_far_vector([0.0, 1.0]))
        )
        document_id = run(
            _add_document_with_embedding(
                session_factory, monitor_id=monitor_id, embedding=_far_vector([0.0, 1.0])
            )
        )

        run(TopicService(session_factory).reassign(document_id, far_topic_id))

        thresholds = TopicThresholds(assignment_threshold=0.5, merge_threshold=0.75)
        outcome = run(assign_topics(session_factory, monitor_id=monitor_id, thresholds=thresholds))

        # The manually assigned document already has an Assignment row, so it
        # never appears in the residue pool assign_topics reads from.
        assert outcome.assigned == 0

        async def load() -> Assignment:
            async with session_factory() as session:
                result = await session.execute(
                    select(Assignment).where(Assignment.document_id == document_id)
                )
                return result.scalar_one()

        assignment = run(load())
        assert assignment.topic_id == far_topic_id
        assert assignment.method is AssignmentMethod.MANUAL


class TestRefit:
    def test_a_refit_below_agreement_is_recorded_not_applied(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_a = run(_add_topic(session_factory, monitor_id=monitor_id, centroid=_far_vector([1.0, 0.0])))
        topic_b = run(_add_topic(session_factory, monitor_id=monitor_id, centroid=_far_vector([0.0, 1.0])))

        document_ids = []
        for _ in range(5):
            document_ids.append(
                run(
                    _add_document_with_embedding(
                        session_factory, monitor_id=monitor_id, embedding=_far_vector([1.0, 0.0])
                    )
                )
            )
        async def write_assignments() -> None:
            async with session_factory() as session:
                for document_id in document_ids:
                    session.add(
                        Assignment(
                            id=uuid.uuid4(), document_id=document_id, topic_id=topic_a,
                            similarity=0.9, method=AssignmentMethod.CENTROID,
                        )
                    )
                await session.commit()
        run(write_assignments())

        class DisagreeingClusterer:
            def cluster(self, reduced: list[list[float]]) -> list[int]:
                # Splits the same five documents into two clusters that
                # disagree with the single-topic assignment above.
                return [0, 1, 0, 1, 0]

        thresholds = TopicThresholds(refit_agreement_threshold=0.9, min_cluster_size=2)
        outcome = run(
            refit_registry(
                session_factory, monitor_id=monitor_id, thresholds=thresholds,
                clusterer=DisagreeingClusterer(),
            )
        )

        assert outcome.applied is False
        assert outcome.reason == "agreement_below_threshold"

        async def unchanged() -> list[uuid.UUID]:
            async with session_factory() as session:
                result = await session.execute(
                    select(Assignment.topic_id).where(Assignment.document_id.in_(document_ids))
                )
                return [row[0] for row in result.all()]

        assert set(run(unchanged())) == {topic_a}


class TestSplitProposal:
    def test_a_split_proposal_is_recorded_not_performed(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_id = run(_add_topic(session_factory, monitor_id=monitor_id, centroid=_far_vector([1.0, 0.0])))

        proposal_id = uuid.uuid4()
        async def add_proposal() -> None:
            async with session_factory() as session:
                session.add(
                    SplitProposal(
                        id=proposal_id, topic_id=topic_id, evidence="high drift", drift_score=0.9,
                    )
                )
                await session.commit()
        run(add_proposal())

        run(TopicService(session_factory).reject_split_proposal(proposal_id))

        async def load() -> tuple[SplitProposal, Topic]:
            async with session_factory() as session:
                proposal = await session.get(SplitProposal, proposal_id)
                topic = await session.get(Topic, topic_id)
                return proposal, topic

        proposal, topic = run(load())
        assert proposal.status is SplitProposalStatus.DISMISSED
        # The topic itself is untouched: no split was performed.
        assert topic.status is TopicStatus.ACTIVE


class TestOverrides:
    def test_merging_into_an_inactive_topic_is_rejected(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_topic = run(_add_topic(session_factory, monitor_id=monitor_id, centroid=_far_vector([1.0, 0.0])))
        target_topic = run(_add_topic(session_factory, monitor_id=monitor_id, centroid=_far_vector([0.0, 1.0])))

        run(TopicService(session_factory).merge(source_topic, target_topic))

        with pytest.raises(MergeTargetInactive):
            run(TopicService(session_factory).merge(target_topic, source_topic))
