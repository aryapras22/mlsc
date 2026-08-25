"""Core tests for insight generation. The router is a deterministic fake
returning fixed structured output, so no network call occurs (design.md,
"Dependencies, injected").

Requirements: 2, 4, 5, 6, 7, 8.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any

import pytest
from sqlalchemy import pool, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.db.models import (
    Assignment,
    AssignmentMethod,
    Base,
    DailyMetric,
    Document,
    Enrichment,
    GenerationSkip,
    Insight,
    InsightKind,
    IngestionRun,
    RollupReason,
    RunStatus,
    SkipReason,
    SourceName,
    TargetType,
    Topic,
    TrendScore,
)
from mlsc.llm.base import Completion, SchemaViolation
from mlsc.pipeline.insights.prompts import DigestOutput, OpportunityOutput
from mlsc.pipeline.normalize import hash_author, hash_content
from mlsc.repositories.insights import InsightRepository
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.tasks.insights import generate_insights

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
    """Returns a fixed opportunity or digest completion. Records every call
    so a test can assert the model was never called per document."""

    def __init__(self, *, opportunity: OpportunityOutput | None = None, digest: DigestOutput | None = None) -> None:
        self._opportunity = opportunity
        self._digest = digest
        self.calls: list[str] = []

    async def complete(self, *, prompt: str, schema: type, prompt_version: str) -> Completion:
        self.calls.append(schema.__name__)
        value = self._opportunity if schema is OpportunityOutput else self._digest
        return Completion(value=value, provider="fake", model="fake-model", prompt_version=prompt_version)


class SchemaViolatingProvider:
    """Simulates a provider whose own single retry (openai_compatible.py) is
    already exhausted — this layer only ever sees the resulting
    ``SchemaViolation``, never the retry itself."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, prompt: str, schema: type, prompt_version: str) -> Completion:
        self.calls += 1
        raise SchemaViolation("output never validated")


class FakeRouter:
    def __init__(self, provider: FakeProvider) -> None:
        self._provider = provider

    def for_tier(self, tier: Any) -> FakeProvider:
        return self._provider


class FakeCache:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], dict] = {}

    async def get(self, content_hash: str, prompt_version: str) -> dict | None:
        return self.store.get((content_hash, prompt_version))

    async def put(self, content_hash: str, prompt_version: str, value: dict) -> None:
        self.store[(content_hash, prompt_version)] = value


async def _make_monitor(session_factory: async_sessionmaker) -> uuid.UUID:
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Roblox", target_type=TargetType.PRODUCT, seed={"identifiers": ["x"]},
            cron_expression="0 3 * * *", timezone="UTC", retention_days=90,
        )
    )
    return monitor.id


_BASE = date(2026, 8, 1)
_PERIOD_END = _BASE + timedelta(days=7)


async def _seed_trustworthy_topic(
    session_factory: async_sessionmaker,
    *,
    monitor_id: uuid.UUID,
    topic_id: uuid.UUID,
    representative_count: int,
    with_gated_change: bool,
) -> None:
    """A topic with a trustworthy day, a metric row, and optionally a
    surviving trend score and enough representatives to pass gating."""
    async with session_factory() as session:
        session.add(
            Topic(
                id=topic_id, monitor_id=monitor_id, label="Login crashes", keywords=[],
                centroid=[0.1] * 384, doc_count=0, first_seen=_BASE, last_seen=_BASE,
            )
        )
        await session.flush()

        session.add(IngestionRun(id=uuid.uuid4(), monitor_id=monitor_id, run_date=_BASE, status=RunStatus.COMPLETE))

        session.add(
            DailyMetric(
                id=uuid.uuid4(), monitor_id=monitor_id, topic_id=topic_id, source_name=None,
                bucket=_BASE, doc_count=10, doc_count_share=0.6, sample_size=10,
                sentiment_mean=-0.5, negativity_rate=0.7, intent_counts={"bug_report": 6, "praise": 4},
                author_diversity=0.8, reason=RollupReason.SCHEDULED,
            )
        )
        session.add(
            DailyMetric(
                id=uuid.uuid4(), monitor_id=monitor_id, topic_id=None, source_name=SourceName.PLAY,
                bucket=_BASE, doc_count=10, doc_count_share=1.0, sample_size=10, author_diversity=0.5,
                reason=RollupReason.SCHEDULED,
            )
        )
        if representative_count:
            session.add(
                DailyMetric(
                    id=uuid.uuid4(), monitor_id=monitor_id, topic_id=topic_id, source_name=SourceName.PLAY,
                    bucket=_BASE, doc_count=10, doc_count_share=1.0, sample_size=10, author_diversity=0.5,
                    reason=RollupReason.SCHEDULED,
                )
            )

        if with_gated_change:
            session.add(
                TrendScore(
                    id=uuid.uuid4(), monitor_id=monitor_id, topic_id=topic_id, bucket=_BASE,
                    value=0.75, components={}, penalties={},
                )
            )

        for index in range(representative_count):
            document_id = uuid.uuid4()
            session.add(
                Document(
                    id=document_id, monitor_id=monitor_id, source_name=SourceName.PLAY,
                    external_id=f"ext-{index}", entity_id="x", author_hash=hash_author("u"),
                    body=f"The app keeps crashing on login. Report {index}.",
                    published_at=datetime(2026, 8, 1, tzinfo=dt_timezone.utc), rating=2,
                    content_hash=hash_content(str(document_id)), raw={},
                )
            )
            await session.flush()
            session.add(
                Enrichment(
                    id=uuid.uuid4(), document_id=document_id, is_relevant=True,
                    embedding=[0.1] * 384, model_versions={},
                )
            )
            session.add(
                Assignment(
                    id=uuid.uuid4(), document_id=document_id, topic_id=topic_id,
                    method=AssignmentMethod.CENTROID, similarity=0.9,
                )
            )
        await session.commit()


def _real_document_id(session_factory: async_sessionmaker, monitor_id: uuid.UUID) -> str:
    async def load() -> str:
        async with session_factory() as session:
            result = await session.execute(select(Document.id).where(Document.monitor_id == monitor_id).limit(1))
            return str(result.scalar_one())

    return run(load())


class TestGrounding:
    def test_a_completion_citing_a_document_outside_its_context_is_discarded(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_id = uuid.uuid4()
        run(
            _seed_trustworthy_topic(
                session_factory, monitor_id=monitor_id, topic_id=topic_id,
                representative_count=3, with_gated_change=True,
            )
        )

        invented_output = OpportunityOutput(
            title="t", who="w", what="w", why="w", body="b", evidence_ids=[str(uuid.uuid4())],
        )
        provider = FakeProvider(opportunity=invented_output)
        router = FakeRouter(provider)

        outcome = run(
            generate_insights(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_PERIOD_END,
                llm_router=router,
            )
        )

        assert outcome.opportunities_written == 0
        assert outcome.skips_recorded == 1

        async def load_skips() -> list[GenerationSkip]:
            async with session_factory() as session:
                result = await session.execute(select(GenerationSkip).where(GenerationSkip.monitor_id == monitor_id))
                return list(result.scalars().all())

        skips = run(load_skips())
        assert [skip.reason for skip in skips] == [SkipReason.GENERATION_FAILED]

    def test_a_valid_completion_is_written_with_its_real_evidence(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_id = uuid.uuid4()
        run(
            _seed_trustworthy_topic(
                session_factory, monitor_id=monitor_id, topic_id=topic_id,
                representative_count=3, with_gated_change=True,
            )
        )
        real_document_id = _real_document_id(session_factory, monitor_id)

        valid_output = OpportunityOutput(
            title="Fix login crashes", who="users", what="crashes on login", why="frustration",
            body="body", evidence_ids=[real_document_id],
        )
        digest_output = DigestOutput(body="digest body")
        provider = FakeProvider(opportunity=valid_output, digest=digest_output)
        router = FakeRouter(provider)

        outcome = run(
            generate_insights(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_PERIOD_END,
                llm_router=router,
            )
        )

        assert outcome.opportunities_written == 1
        assert outcome.digest_written is True

        async def load_insights() -> list[Insight]:
            async with session_factory() as session:
                return await InsightRepository(session).list_for_period(
                    monitor_id, period_start=_BASE, period_end=_PERIOD_END, kind=InsightKind.OPPORTUNITY
                )

        insights = run(load_insights())
        assert len(insights) == 1
        assert insights[0].evidence_ids == [real_document_id]
        assert insights[0].llm_provider == "fake"


class TestGating:
    def test_an_untrustworthy_day_is_skipped(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_id = uuid.uuid4()
        run(
            _seed_trustworthy_topic(
                session_factory, monitor_id=monitor_id, topic_id=topic_id,
                representative_count=3, with_gated_change=True,
            )
        )

        async def mark_partial() -> None:
            async with session_factory() as session:
                result = await session.execute(select(IngestionRun).where(IngestionRun.monitor_id == monitor_id))
                run_row = result.scalar_one()
                run_row.status = RunStatus.PARTIAL
                await session.commit()

        run(mark_partial())

        router = FakeRouter(FakeProvider())
        outcome = run(
            generate_insights(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_PERIOD_END,
                llm_router=router,
            )
        )

        assert outcome.opportunities_written == 0
        assert outcome.skips_recorded == 1
        assert router._provider.calls == []

        async def load_skips() -> list[GenerationSkip]:
            async with session_factory() as session:
                result = await session.execute(select(GenerationSkip).where(GenerationSkip.monitor_id == monitor_id))
                return list(result.scalars().all())

        skips = run(load_skips())
        assert [skip.reason for skip in skips] == [SkipReason.DAY_UNTRUSTWORTHY]

    def test_evidence_too_thin_is_skipped_without_calling_the_model(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_id = uuid.uuid4()
        run(
            _seed_trustworthy_topic(
                session_factory, monitor_id=monitor_id, topic_id=topic_id,
                representative_count=2, with_gated_change=True,
            )
        )

        router = FakeRouter(FakeProvider())
        outcome = run(
            generate_insights(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_PERIOD_END,
                llm_router=router,
            )
        )

        assert outcome.opportunities_written == 0
        assert router._provider.calls == []

        async def load_skips() -> list[GenerationSkip]:
            async with session_factory() as session:
                result = await session.execute(select(GenerationSkip).where(GenerationSkip.monitor_id == monitor_id))
                return list(result.scalars().all())

        skips = run(load_skips())
        assert [skip.reason for skip in skips] == [SkipReason.EVIDENCE_TOO_THIN]

    def test_no_gated_change_is_skipped(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_id = uuid.uuid4()
        run(
            _seed_trustworthy_topic(
                session_factory, monitor_id=monitor_id, topic_id=topic_id,
                representative_count=3, with_gated_change=False,
            )
        )

        router = FakeRouter(FakeProvider())
        outcome = run(
            generate_insights(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_PERIOD_END,
                llm_router=router,
            )
        )

        assert outcome.opportunities_written == 0

        async def load_skips() -> list[GenerationSkip]:
            async with session_factory() as session:
                result = await session.execute(select(GenerationSkip).where(GenerationSkip.monitor_id == monitor_id))
                return list(result.scalars().all())

        skips = run(load_skips())
        assert [skip.reason for skip in skips] == [SkipReason.NO_CHANGE_DETECTED]


class TestCaching:
    def test_a_second_run_over_the_same_context_makes_no_call(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_id = uuid.uuid4()
        run(
            _seed_trustworthy_topic(
                session_factory, monitor_id=monitor_id, topic_id=topic_id,
                representative_count=3, with_gated_change=True,
            )
        )
        real_document_id = _real_document_id(session_factory, monitor_id)

        valid_output = OpportunityOutput(
            title="Fix login crashes", who="users", what="crashes", why="frustration",
            body="body", evidence_ids=[real_document_id],
        )
        digest_output = DigestOutput(body="digest body")
        provider = FakeProvider(opportunity=valid_output, digest=digest_output)
        router = FakeRouter(provider)
        cache = FakeCache()

        run(
            generate_insights(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_PERIOD_END,
                llm_router=router, cache=cache,
            )
        )
        opportunity_calls_after_first_run = provider.calls.count("OpportunityOutput")
        assert opportunity_calls_after_first_run == 1

        run(
            generate_insights(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_PERIOD_END,
                llm_router=router, cache=cache,
            )
        )

        # The digest step is not cached (design.md: cache lives inside the
        # per-topic loop only), so only the opportunity call is suppressed.
        assert provider.calls.count("OpportunityOutput") == 1


class TestScoringAndProvenance:
    def test_every_written_insight_carries_provenance(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_id = uuid.uuid4()
        run(
            _seed_trustworthy_topic(
                session_factory, monitor_id=monitor_id, topic_id=topic_id,
                representative_count=3, with_gated_change=True,
            )
        )
        real_document_id = _real_document_id(session_factory, monitor_id)

        valid_output = OpportunityOutput(
            title="Fix login crashes", who="users", what="crashes", why="frustration",
            body="body", evidence_ids=[real_document_id],
        )
        digest_output = DigestOutput(body="digest body")
        router = FakeRouter(FakeProvider(opportunity=valid_output, digest=digest_output))

        run(
            generate_insights(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_PERIOD_END,
                llm_router=router,
            )
        )

        async def load_all() -> list[Insight]:
            async with session_factory() as session:
                result = await session.execute(select(Insight).where(Insight.monitor_id == monitor_id))
                return list(result.scalars().all())

        insights = run(load_all())
        assert len(insights) == 2  # one opportunity, one digest
        for insight in insights:
            assert insight.llm_provider == "fake"
            assert insight.llm_model == "fake-model"
            assert insight.prompt_version

        opportunity = next(insight for insight in insights if insight.kind == InsightKind.OPPORTUNITY)
        assert opportunity.score is not None
        assert set(opportunity.score_components) == {
            "frequency", "severity", "momentum", "breadth", "intent_purity", "staleness",
        }


class TestMalformedOutput:
    def test_malformed_output_is_recorded_as_failed_and_nothing_persisted(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        topic_id = uuid.uuid4()
        run(
            _seed_trustworthy_topic(
                session_factory, monitor_id=monitor_id, topic_id=topic_id,
                representative_count=3, with_gated_change=True,
            )
        )

        provider = SchemaViolatingProvider()
        router = FakeRouter(provider)

        outcome = run(
            generate_insights(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_PERIOD_END,
                llm_router=router,
            )
        )

        assert outcome.opportunities_written == 0
        assert provider.calls == 1

        async def load() -> tuple[list[Insight], list[GenerationSkip]]:
            async with session_factory() as session:
                insights = (await session.execute(select(Insight).where(Insight.monitor_id == monitor_id))).scalars().all()
                skips = (await session.execute(select(GenerationSkip).where(GenerationSkip.monitor_id == monitor_id))).scalars().all()
                return list(insights), list(skips)

        insights, skips = run(load())
        assert insights == []
        assert [skip.reason for skip in skips] == [SkipReason.GENERATION_FAILED]


class TestModelCallVolume:
    def test_the_model_is_never_called_per_document(self, session_factory: async_sessionmaker) -> None:
        """Requirement 4: one call per topic, not one per document — a topic
        with many representatives still produces exactly one opportunity call."""
        monitor_id = run(_make_monitor(session_factory))
        topic_id = uuid.uuid4()
        run(
            _seed_trustworthy_topic(
                session_factory, monitor_id=monitor_id, topic_id=topic_id,
                representative_count=8, with_gated_change=True,
            )
        )
        real_document_id = _real_document_id(session_factory, monitor_id)

        valid_output = OpportunityOutput(
            title="t", who="w", what="w", why="w", body="b", evidence_ids=[real_document_id],
        )
        provider = FakeProvider(opportunity=valid_output, digest=DigestOutput(body="d"))
        router = FakeRouter(provider)

        run(
            generate_insights(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_PERIOD_END,
                llm_router=router,
            )
        )

        assert provider.calls.count("OpportunityOutput") == 1
