"""Tests for document enrichment. No network call.

Requirements: 1, 2, 4, 5, 6, 8, 9.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import pool, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.db.models import (
    Base,
    Document,
    Enrichment,
    SentimentLabel,
    SourceName,
    TargetType,
)
from mlsc.llm.base import Completion, SchemaViolation
from mlsc.llm.cache import LlmResponseCache
from mlsc.llm.router import LlmRouter, Tier
from mlsc.pipeline.duplicate import hamming_distance, is_near_duplicate, simhash
from mlsc.pipeline.enrich import Embedder, SentimentScorer
from mlsc.pipeline.intent import IntentBatchResult, IntentResult
from mlsc.pipeline.normalize import hash_content
from mlsc.tasks.enrich import Stage, enrich_documents

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


class FixedEmbedder(Embedder):
    """No model load: a fixed-length vector regardless of input."""

    def __init__(self) -> None:
        pass

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 384 for _ in texts]


class FakeProvider:
    """Returns a queued completion, or replays validation failures, per call."""

    def __init__(self, completions: list[Completion | Exception]) -> None:
        self._completions = list(completions)
        self.calls = 0

    async def complete(self, *, prompt: str, schema: type, prompt_version: str) -> Completion:
        self.calls += 1
        outcome = self._completions.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _make_router(provider: FakeProvider) -> LlmRouter:
    return LlmRouter({Tier.INTENT: provider, Tier.LABELING: provider, Tier.INSIGHT: provider})


class FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value


async def _add_document(session_factory: async_sessionmaker, *, monitor_id: uuid.UUID, body: str) -> uuid.UUID:
    document_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Document(
                id=document_id,
                monitor_id=monitor_id,
                source_name=SourceName.PLAY,
                external_id=str(document_id),
                entity_id="com.example.app",
                url=None,
                author_hash="hash",
                body=body,
                published_at=datetime.now(timezone.utc),
                rating=5,
                app_version="1.0",
                engagement=None,
                content_hash=hash_content(body),
                raw={},
            )
        )
        await session.commit()
    return document_id


async def _make_monitor(session_factory: async_sessionmaker) -> uuid.UUID:
    from mlsc.application.monitors import MonitorService
    from mlsc.schemas.monitors import MonitorCreateRequest

    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Roblox",
            target_type=TargetType.PRODUCT,
            seed={"identifiers": ["com.roblox.client"]},
            cron_expression="0 3 * * *",
            timezone="UTC",
            retention_days=90,
        )
    )
    return monitor.id


async def _load_enrichment(session_factory: async_sessionmaker, document_id: uuid.UUID) -> Enrichment:
    async with session_factory() as session:
        result = await session.execute(
            select(Enrichment).where(Enrichment.document_id == document_id)
        )
        return result.scalar_one()


class TestSimhash:
    def test_near_identical_text_is_flagged_a_near_duplicate(self) -> None:
        a = simhash("This game is amazing, I love the new update! Best game ever.")
        b = simhash("This game is amazing, I love the new update! Best game ever!!")
        assert is_near_duplicate(a, b)

    def test_unrelated_text_is_not_a_near_duplicate(self) -> None:
        a = simhash("This game is amazing, I love the new update! Best game ever.")
        b = simhash("Terrible lag and constant crashes, please fix this bug ASAP.")
        assert not is_near_duplicate(a, b)


class TestEnrichmentPipeline:
    def test_pii_is_stripped_and_author_stays_a_hash(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        document_id = run(
            _add_document(
                session_factory,
                monitor_id=monitor_id,
                body="Contact me at test@example.com or call 555-123-4567 for details.",
            )
        )

        run(
            enrich_documents(
                session_factory,
                monitor_id=monitor_id,
                stages=frozenset({Stage.CLEAN}),
                embedder=FixedEmbedder(),
                sentiment_scorer=SentimentScorer(),
            )
        )

        async def load_document() -> Document:
            async with session_factory() as session:
                return await session.get(Document, document_id)

        document = run(load_document())
        assert "test@example.com" not in document.body
        assert "555-123-4567" not in document.body

    def test_a_filtered_language_is_counted_not_deleted(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        document_id = run(
            _add_document(
                session_factory, monitor_id=monitor_id, body="Ceci est un excellent jeu, je l'adore vraiment beaucoup."
            )
        )

        written = run(
            enrich_documents(
                session_factory,
                monitor_id=monitor_id,
                stages=frozenset({Stage.CLEAN, Stage.LANGUAGE}),
                embedder=FixedEmbedder(),
                sentiment_scorer=SentimentScorer(),
                accepted_languages=frozenset({"en"}),
            )
        )
        assert written == 1

        enrichment = run(_load_enrichment(session_factory, document_id))
        assert enrichment.is_relevant is False

        async def document_still_exists() -> Document | None:
            async with session_factory() as session:
                return await session.get(Document, document_id)

        assert run(document_still_exists()) is not None

    def test_a_near_duplicate_is_flagged_and_stays_readable(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        first_id = run(
            _add_document(
                session_factory,
                monitor_id=monitor_id,
                body="This game is amazing, I love the new update! Best game ever.",
            )
        )
        second_id = run(
            _add_document(
                session_factory,
                monitor_id=monitor_id,
                body="This game is amazing, I love the new update! Best game ever!!",
            )
        )

        run(
            enrich_documents(
                session_factory,
                monitor_id=monitor_id,
                stages=frozenset({Stage.CLEAN, Stage.DUPLICATE}),
                embedder=FixedEmbedder(),
                sentiment_scorer=SentimentScorer(),
            )
        )

        second_enrichment = run(_load_enrichment(session_factory, second_id))
        assert second_enrichment.near_duplicate_of == first_id

        async def second_document_readable() -> Document:
            async with session_factory() as session:
                return await session.get(Document, second_id)

        assert run(second_document_readable()).body is not None

    def test_a_single_stage_rerun_touches_only_that_stage(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        document_id = run(
            _add_document(session_factory, monitor_id=monitor_id, body="A perfectly good review of the app.")
        )

        run(
            enrich_documents(
                session_factory,
                monitor_id=monitor_id,
                stages=frozenset({Stage.CLEAN, Stage.SENTIMENT}),
                embedder=FixedEmbedder(),
                sentiment_scorer=SentimentScorer(),
            )
        )
        first_pass = run(_load_enrichment(session_factory, document_id))
        assert first_pass.sentiment_label is not None
        assert first_pass.embedding is None

        run(
            enrich_documents(
                session_factory,
                monitor_id=monitor_id,
                stages=frozenset({Stage.CLEAN, Stage.EMBED}),
                embedder=FixedEmbedder(),
                sentiment_scorer=SentimentScorer(),
            )
        )
        second_pass = run(_load_enrichment(session_factory, document_id))
        assert second_pass.embedding is not None
        assert second_pass.sentiment_label == first_pass.sentiment_label


class TestIntentClassification:
    def test_malformed_completion_retried_once_then_task_fails_with_nothing_persisted(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        document_id = run(
            _add_document(session_factory, monitor_id=monitor_id, body="A perfectly good review of the app.")
        )
        provider = FakeProvider([SchemaViolation("bad json"), SchemaViolation("bad json")])
        router = _make_router(provider)

        with pytest.raises(SchemaViolation):
            run(
                enrich_documents(
                    session_factory,
                    monitor_id=monitor_id,
                    stages=frozenset({Stage.CLEAN, Stage.INTENT}),
                    embedder=FixedEmbedder(),
                    sentiment_scorer=SentimentScorer(),
                    llm_router=router,
                )
            )

        # The whole batch's transaction never commits, so not even the
        # partially-built enrichment row for this document survives.
        async def enrichment_count() -> int:
            async with session_factory() as session:
                result = await session.execute(
                    select(Enrichment).where(Enrichment.document_id == document_id)
                )
                return len(result.scalars().all())

        assert run(enrichment_count()) == 0

    def test_no_llm_call_is_repeated_for_an_unchanged_document_and_prompt_version(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        document_id = run(
            _add_document(session_factory, monitor_id=monitor_id, body="A perfectly good review of the app.")
        )
        completion = Completion(
            value=IntentBatchResult(
                results=[IntentResult(document_id=str(document_id), intent="praise", confidence=0.9)]
            ),
            provider="fake", model="fake-model", prompt_version="v1",
        )
        provider = FakeProvider([completion])
        router = _make_router(provider)
        redis = FakeRedis()
        cache = LlmResponseCache(redis)

        run(
            enrich_documents(
                session_factory,
                monitor_id=monitor_id,
                stages=frozenset({Stage.CLEAN, Stage.INTENT}),
                embedder=FixedEmbedder(),
                sentiment_scorer=SentimentScorer(),
                llm_router=router,
                llm_cache=cache,
            )
        )
        assert provider.calls == 1

        run(
            enrich_documents(
                session_factory,
                monitor_id=monitor_id,
                stages=frozenset({Stage.CLEAN, Stage.INTENT}),
                embedder=FixedEmbedder(),
                sentiment_scorer=SentimentScorer(),
                llm_router=router,
                llm_cache=cache,
            )
        )
        assert provider.calls == 1

        enrichment = run(_load_enrichment(session_factory, document_id))
        assert enrichment.intent == "praise"


class TestProviderIsolation:
    def test_no_module_outside_the_provider_package_and_router_names_a_provider(self) -> None:
        """The router is the one assembly point permitted to name a concrete
        provider (design.md: "built once at startup"); every other caller
        reaches a provider only through ``LlmRouter.for_tier``."""
        import ast
        from pathlib import Path

        mlsc_root = Path(__file__).parent.parent / "mlsc"
        allowed = {"llm/providers", "llm/router.py"}
        offending: list[str] = []
        for path in mlsc_root.rglob("*.py"):
            if any(marker in str(path) for marker in allowed):
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "providers" in node.module:
                    offending.append(str(path))
        assert not offending, f"provider named outside the provider package: {offending}"
