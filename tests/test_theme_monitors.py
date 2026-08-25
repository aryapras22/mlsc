"""Core tests for theme monitors. No network call except where noted.

Requirements: 1, 2, 3, 4, 5, 6, 7, 8.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import pool, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.application.sources import MonitorSourceService
from mlsc.application.themes import CandidateNotViable, ReviewedQuery, ThemeService
from mlsc.db.models import (
    Base,
    CandidateState,
    Document,
    DiscoverySurface,
    Enrichment,
    EntityCandidate,
    MonitorSource,
    RelevanceBasis,
    SourceName,
    TargetType,
    ThemeSeed,
)
from mlsc.llm.base import Completion
from mlsc.pipeline.normalize import hash_content
from mlsc.pipeline.relevance import ThemeRelevanceScorer
from mlsc.pipeline.themes import GeneratedQuery, GeneratedQuerySet, QueriesUnusable, generate_theme_queries
from mlsc.repositories.themes import EntityCandidateRepository, ThemeSeedRepository
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.schemas.sources import MonitorSourceCreateRequest
from mlsc.tasks.enrich import Stage, ThemeRelevanceContext, enrich_documents
from mlsc.tasks.themes import run_discovery, run_query_generation

LOCAL_DATABASE_URL = "postgresql+asyncpg://mlsc:mlsc@localhost:55433/mlsc_test"


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
        pytest.skip("local Compose PostgreSQL is not reachable at localhost:55433/mlsc_test")
    run(_reset_schema(engine))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    run(engine.dispose())


class FakeProvider:
    """Returns a queued completion, or replays an error, per call."""

    def __init__(self, completions: list[Completion | Exception]) -> None:
        self._completions = list(completions)

    async def complete(self, *, prompt: str, schema: type, prompt_version: str) -> Completion:
        outcome = self._completions.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _query_completion(*queries: tuple[str, str]) -> Completion:
    return Completion(
        value=GeneratedQuerySet(
            queries=[GeneratedQuery(text=text, rationale=rationale) for text, rationale in queries]
        ),
        provider="fake", model="fake-model", prompt_version="v1",
    )


class FakeRouter:
    """Enough of ``LlmRouter`` for ``generate_theme_queries``: a single
    provider regardless of tier."""

    def __init__(self, provider: FakeProvider) -> None:
        self._provider = provider

    def for_tier(self, tier: object) -> FakeProvider:
        return self._provider


async def _make_theme_monitor(session_factory: async_sessionmaker, *, description: str) -> uuid.UUID:
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="AI note-taking apps",
            target_type=TargetType.THEME,
            seed={"description": description},
            cron_expression="0 3 * * *",
            timezone="UTC",
            retention_days=90,
        )
    )
    return monitor.id


async def _add_document(
    session_factory: async_sessionmaker, *, monitor_id: uuid.UUID, body: str
) -> uuid.UUID:
    document_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Document(
                id=document_id, monitor_id=monitor_id, source_name=SourceName.APPSTORE,
                external_id=str(document_id), entity_id="123", url=None, author_hash="hash",
                body=body, published_at=datetime.now(timezone.utc), rating=5, app_version=None,
                engagement=None, content_hash=hash_content(body), raw={},
            )
        )
        await session.commit()
    return document_id


class FixedVectorEmbedder:
    """No model load. Returns the vector registered for a given text, or a
    default far from every registered vector."""

    def __init__(self, vectors: dict[str, list[float]], *, default: list[float]) -> None:
        self._vectors = vectors
        self._default = default

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors.get(text, self._default) for text in texts]


class TestThemeCreation:
    def test_a_theme_is_created_from_a_description_alone(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_theme_monitor(session_factory, description="the AI note-taking app space"))

        async def load_seed() -> ThemeSeed:
            async with session_factory() as session:
                return await ThemeSeedRepository(session).get_by_monitor(monitor_id)

        seed = run(load_seed())
        assert seed.description == "the AI note-taking app space"
        assert seed.queries == []


class TestQueryGeneration:
    def test_queries_are_stored_unaccepted_until_reviewed(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_theme_monitor(session_factory, description="note-taking apps"))
        provider = FakeProvider([_query_completion(("ai note taking app", "direct match"))])

        run(run_query_generation(session_factory, monitor_id=monitor_id, llm_router=FakeRouter(provider)))

        async def load_seed() -> ThemeSeed:
            async with session_factory() as session:
                return await ThemeSeedRepository(session).get_by_monitor(monitor_id)

        seed = run(load_seed())
        assert len(seed.queries) == 1
        assert seed.queries[0]["accepted"] is False

    def test_an_unusable_expansion_is_recorded_rather_than_invented_around(self) -> None:
        provider = FakeProvider([_query_completion()])  # empty queries list

        with pytest.raises(QueriesUnusable):
            run(generate_theme_queries(FakeRouter(provider), "an empty theme"))

    def test_review_replaces_the_query_set_and_marks_every_one_accepted(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_theme_monitor(session_factory, description="note-taking apps"))
        provider = FakeProvider(
            [_query_completion(("note app", "generic"), ("ai note taking app", "specific"))]
        )
        run(run_query_generation(session_factory, monitor_id=monitor_id, llm_router=FakeRouter(provider)))

        run(
            ThemeService(session_factory).review_queries(
                monitor_id, [ReviewedQuery(text="ai note taking app", rationale="specific")]
            )
        )

        async def load_seed() -> ThemeSeed:
            async with session_factory() as session:
                return await ThemeSeedRepository(session).get_by_monitor(monitor_id)

        seed = run(load_seed())
        assert [q["text"] for q in seed.queries] == ["ai note taking app"]
        assert all(q["accepted"] for q in seed.queries)


class TestCandidateReview:
    async def _seed_accepted_candidate(
        self, session_factory: async_sessionmaker, monitor_id: uuid.UUID, *, entity_ref: str = "999"
    ) -> uuid.UUID:
        candidate_id = uuid.uuid4()
        async with session_factory() as session:
            session.add(
                EntityCandidate(
                    id=candidate_id, monitor_id=monitor_id, source_name=SourceName.APPSTORE,
                    entity_ref=entity_ref, display_name="Some App", reason="matched query",
                    proposed_by_query="note app", state=CandidateState.PROPOSED,
                    provenance={"surface": DiscoverySurface.APP_STORE_SEARCH.value},
                )
            )
            await session.commit()
        return candidate_id

    def test_a_proposed_candidate_contributes_no_source_and_no_figures(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_theme_monitor(session_factory, description="note-taking apps"))
        run(self._seed_accepted_candidate(session_factory, monitor_id))

        async def sources_for_monitor() -> list[MonitorSource]:
            async with session_factory() as session:
                result = await session.execute(
                    select(MonitorSource).where(MonitorSource.monitor_id == monitor_id)
                )
                return list(result.scalars().all())

        assert run(sources_for_monitor()) == []

    def test_acceptance_produces_an_ordinary_attached_source(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_theme_monitor(session_factory, description="note-taking apps"))
        candidate_id = run(self._seed_accepted_candidate(session_factory, monitor_id, entity_ref="123456"))

        run(ThemeService(session_factory).accept_candidate(monitor_id, candidate_id))

        async def load() -> tuple[MonitorSource, EntityCandidate]:
            async with session_factory() as session:
                sources = (
                    await session.execute(
                        select(MonitorSource).where(MonitorSource.monitor_id == monitor_id)
                    )
                ).scalars().all()
                candidate = await EntityCandidateRepository(session).get(candidate_id)
                return sources[0], candidate

        source, candidate = run(load())
        assert source.source_name is SourceName.APPSTORE
        assert source.config == {"app_id": "123456"}
        assert candidate.state is CandidateState.ACCEPTED

    def test_a_nonviable_candidate_is_left_proposed_with_the_reason_attached(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_theme_monitor(session_factory, description="note-taking apps"))
        # not_a_number is not a valid App Store numeric id: _validate_appstore_config rejects it.
        candidate_id = run(
            self._seed_accepted_candidate(session_factory, monitor_id, entity_ref="not_a_number")
        )

        with pytest.raises(CandidateNotViable):
            run(ThemeService(session_factory).accept_candidate(monitor_id, candidate_id))

        async def load_candidate() -> EntityCandidate:
            async with session_factory() as session:
                return await EntityCandidateRepository(session).get(candidate_id)

        assert run(load_candidate()).state is CandidateState.PROPOSED

    def test_a_rejected_candidate_is_permanent_and_not_reproposed(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_theme_monitor(session_factory, description="note-taking apps"))
        candidate_id = run(self._seed_accepted_candidate(session_factory, monitor_id, entity_ref="123456"))

        run(ThemeService(session_factory).reject_candidate(monitor_id, candidate_id))

        async def rejected_refs() -> set[str]:
            async with session_factory() as session:
                return await EntityCandidateRepository(session).rejected_entity_refs(
                    monitor_id, SourceName.APPSTORE
                )

        assert "123456" in run(rejected_refs())


class TestDiscoveryOutcome:
    def test_a_theme_with_no_accepted_query_reports_it_has_nothing_to_watch(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_theme_monitor(session_factory, description="note-taking apps"))

        outcome = run(
            run_discovery(
                session_factory, monitor_id=monitor_id, fetch_client=None, resolver=None, extractor=None
            )
        )

        assert outcome.proposed == 0
        assert outcome.reason is not None


class TestRelevanceFiltering:
    def test_an_irrelevant_document_is_filtered_not_deleted(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_theme_monitor(session_factory, description="note-taking apps"))
        on_topic_id = run(_add_document(session_factory, monitor_id=monitor_id, body="a great note taking app"))
        off_topic_id = run(_add_document(session_factory, monitor_id=monitor_id, body="unrelated racing game news"))

        description_vector = [1.0, 0.0]
        embedder = FixedVectorEmbedder(
            {
                "a great note taking app": [0.99, 0.01],
                "unrelated racing game news": [0.0, 1.0],
                "note-taking apps": description_vector,
            },
            default=[0.5, 0.5],
        )
        theme_relevance = ThemeRelevanceContext(
            ThemeRelevanceScorer(threshold=0.5),
            reference_embeddings=[description_vector],
            basis=RelevanceBasis.DESCRIPTION,
        )

        from mlsc.pipeline.enrich import SentimentScorer

        run(
            enrich_documents(
                session_factory, monitor_id=monitor_id,
                stages=frozenset({Stage.CLEAN, Stage.RELEVANCE}),
                embedder=embedder, sentiment_scorer=SentimentScorer(),
                theme_relevance=theme_relevance,
            )
        )

        async def load_enrichments() -> dict[uuid.UUID, Enrichment]:
            async with session_factory() as session:
                result = await session.execute(select(Enrichment))
                return {e.document_id: e for e in result.scalars().all()}

        enrichments = run(load_enrichments())
        assert enrichments[on_topic_id].is_relevant is True
        assert enrichments[off_topic_id].is_relevant is False
        assert enrichments[off_topic_id].relevance_basis is RelevanceBasis.DESCRIPTION

        async def still_present() -> int:
            async with session_factory() as session:
                result = await session.execute(
                    select(Document).where(Document.id == off_topic_id)
                )
                return len(result.scalars().all())

        assert run(still_present()) == 1
