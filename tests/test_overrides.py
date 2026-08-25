"""Tests for operator-initiated repair overrides.

Requirements: 1, 2, 4, 6, 7.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone as tz

import pytest
from sqlalchemy import pool, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.monitors import MonitorService
from mlsc.application.overrides import (
    OverrideOverlaps,
    OverrideService,
    PurgeNotConfirmed,
    preview_token,
)
from mlsc.application.sources import MonitorSourceService
from mlsc.application.runs import SourceOutcome, SourceResult
from mlsc.db.models import (
    Base,
    Document,
    Enrichment,
    FetchStats,
    IngestionRun,
    OverrideKind,
    OverrideStatus,
    QuotaOutcome,
    RunStatus,
    SourceName,
    TargetType,
)
from mlsc.llm.base import Completion
from mlsc.llm.router import LlmRouter, Tier
from mlsc.pipeline.enrich import Embedder, SentimentScorer
from mlsc.pipeline.intent import IntentBatchResult, IntentResult
from mlsc.pipeline.normalize import hash_author, hash_content
from mlsc.pipeline.stages import Stage
from mlsc.schemas.monitors import MonitorCreateRequest
from mlsc.schemas.overrides import OverrideRequest
from mlsc.schemas.sources import MonitorSourceCreateRequest
from mlsc.tasks.overrides import _run_backfill_window, _run_stage_rerun


class FixedEmbedder(Embedder):
    """No model load: a fixed-length vector regardless of input."""

    def __init__(self) -> None:
        pass

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 384 for _ in texts]


class FakeProvider:
    """Returns one queued completion per call, recording how many it served."""

    def __init__(self, completions: list[Completion]) -> None:
        self._completions = list(completions)
        self.calls = 0

    async def complete(self, *, prompt: str, schema: type, prompt_version: str) -> Completion:
        self.calls += 1
        return self._completions.pop(0)


def _make_router(provider: FakeProvider) -> LlmRouter:
    return LlmRouter({Tier.INTENT: provider, Tier.LABELING: provider, Tier.INSIGHT: provider})

LOCAL_DATABASE_URL = "postgresql+asyncpg://mlsc:mlsc@localhost:55433/mlsc_test"


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


async def _reachable(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except (OperationalError, OSError):
        return False


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []

    def dispatch_override(self, job_id: uuid.UUID) -> None:
        self.dispatched.append(job_id)


class _FixedClock:
    def __init__(self, today: date) -> None:
        self._today = today

    def today(self) -> date:
        return self._today


@pytest.fixture
def session_factory():
    engine = create_async_engine(LOCAL_DATABASE_URL, poolclass=pool.NullPool)
    if not run(_reachable(engine)):
        run(engine.dispose())
        pytest.skip("local Compose PostgreSQL is not reachable at localhost:55433/mlsc_test")

    async def reset():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    run(reset())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    run(engine.dispose())


async def _make_monitor(session_factory, retention_days: int = 90) -> uuid.UUID:  # noqa: ANN001
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Roblox", target_type=TargetType.PRODUCT,
            seed={"identifiers": ["com.roblox.client"]},
            cron_expression="0 3 * * *", timezone="UTC", retention_days=retention_days,
        )
    )
    return monitor.id


async def _add_document(session_factory, monitor_id: uuid.UUID, published_at: date) -> uuid.UUID:  # noqa: ANN001
    document_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(Document(
            id=document_id, monitor_id=monitor_id, source_name=SourceName.PLAY,
            external_id=str(uuid.uuid4()), entity_id="x", url=None,
            author_hash=hash_author("a"), body="body",
            published_at=published_at,
            rating=3, app_version=None, engagement=None,
            content_hash=hash_content(str(uuid.uuid4())), raw={},
        ))
        await session.commit()
    return document_id


async def _attach_play_source(session_factory, monitor_id: uuid.UUID) -> uuid.UUID:  # noqa: ANN001
    source = await MonitorSourceService(session_factory).attach(
        monitor_id,
        MonitorSourceCreateRequest(
            source_name=SourceName.PLAY, config={"package_id": "com.roblox.client"}, daily_quota=10
        ),
    )
    return source.id


async def _seed_ledgered_document(session_factory, monitor_id: uuid.UUID, source_id: uuid.UUID) -> uuid.UUID:  # noqa: ANN001
    """A document whose published date has a matching FetchStats ledger row,
    so rollup_daily can recompute its bucket without tripping SampleZero
    (the guard a stage re-run's recompute must not silently bypass)."""
    bucket = date.today()
    published_at = datetime(bucket.year, bucket.month, bucket.day, 12, 0, tzinfo=tz.utc)
    document_id = uuid.uuid4()
    async with session_factory() as session:
        run_id = uuid.uuid4()
        session.add(IngestionRun(id=run_id, monitor_id=monitor_id, run_date=bucket, status=RunStatus.COMPLETE))
        await session.flush()
        session.add(FetchStats(
            id=uuid.uuid4(), run_id=run_id, monitor_source_id=source_id,
            attempted=1, fetched=1, kept=1, quota=10, quota_outcome=QuotaOutcome.WITHIN_ALLOWANCE,
            library_version="test", duration_seconds=0.1,
        ))
        session.add(Document(
            id=document_id, monitor_id=monitor_id, source_name=SourceName.PLAY,
            external_id=str(document_id), entity_id="x", url=None,
            author_hash=hash_author("a"), body="a perfectly good review",
            published_at=published_at,
            rating=5, app_version=None, engagement=None,
            content_hash=hash_content(str(document_id)), raw={},
        ))
        await session.commit()
    return document_id


async def _load_enrichment(session_factory, document_id: uuid.UUID) -> Enrichment:  # noqa: ANN001
    async with session_factory() as session:
        result = await session.execute(select(Enrichment).where(Enrichment.document_id == document_id))
        return result.scalar_one()


class TestOverrideOverlap:
    def test_second_submission_of_same_kind_is_refused(self, session_factory) -> None:
        monitor_id = run(_make_monitor(session_factory))
        service = OverrideService(session_factory, _RecordingDispatcher())
        token = run(service.preview_retention(monitor_id)).token
        request = OverrideRequest(kind=OverrideKind.RETENTION_PURGE, purge_token=token)

        run(service.submit(monitor_id, request))

        with pytest.raises(OverrideOverlaps):
            run(service.submit(monitor_id, request))


class TestRetentionPreview:
    def test_counts_only_documents_past_the_cutoff(self, session_factory) -> None:
        monitor_id = run(_make_monitor(session_factory, retention_days=90))
        run(_add_document(session_factory, monitor_id, date.today() - timedelta(days=200)))
        run(_add_document(session_factory, monitor_id, date.today() - timedelta(days=10)))

        service = OverrideService(session_factory, _RecordingDispatcher())
        preview = run(service.preview_retention(monitor_id))

        assert preview.count == 1

    def test_token_matches_the_cutoff_and_count_it_was_issued_for(self, session_factory) -> None:
        monitor_id = run(_make_monitor(session_factory, retention_days=90))
        run(_add_document(session_factory, monitor_id, date.today() - timedelta(days=200)))

        clock = _FixedClock(date.today())
        service = OverrideService(session_factory, _RecordingDispatcher(), clock=clock)
        preview = run(service.preview_retention(monitor_id))

        cutoff = clock.today() - timedelta(days=90)
        assert preview.token == preview_token(monitor_id, cutoff, 1)

    def test_a_later_call_with_more_documents_yields_a_different_token(self, session_factory) -> None:
        monitor_id = run(_make_monitor(session_factory, retention_days=90))
        run(_add_document(session_factory, monitor_id, date.today() - timedelta(days=200)))

        service = OverrideService(session_factory, _RecordingDispatcher())
        first = run(service.preview_retention(monitor_id))

        run(_add_document(session_factory, monitor_id, date.today() - timedelta(days=201)))
        second = run(service.preview_retention(monitor_id))

        assert first.token != second.token


class TestPurgeConfirmation:
    def test_a_missing_token_is_refused(self, session_factory) -> None:
        monitor_id = run(_make_monitor(session_factory))
        service = OverrideService(session_factory, _RecordingDispatcher())

        with pytest.raises(PurgeNotConfirmed):
            run(service.submit(monitor_id, OverrideRequest(kind=OverrideKind.RETENTION_PURGE)))

    def test_a_token_that_does_not_match_the_current_count_is_refused(self, session_factory) -> None:
        monitor_id = run(_make_monitor(session_factory))
        service = OverrideService(session_factory, _RecordingDispatcher())
        stale_token = preview_token(monitor_id, date.today(), 999)

        with pytest.raises(PurgeNotConfirmed):
            run(
                service.submit(
                    monitor_id,
                    OverrideRequest(kind=OverrideKind.RETENTION_PURGE, purge_token=stale_token),
                )
            )


class TestStageRerun:
    def test_leaves_other_stages_untouched_and_recomputes_the_touched_date(self, session_factory) -> None:
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_play_source(session_factory, monitor_id))
        document_id = run(_seed_ledgered_document(session_factory, monitor_id, source_id))
        router = _make_router(FakeProvider([]))

        run(
            _run_stage_rerun(
                session_factory,
                monitor_id=monitor_id,
                stage=Stage.EMBED,
                embedder=FixedEmbedder(),
                sentiment_scorer=SentimentScorer(),
                llm_router=router,
            )
        )
        after_embed = run(_load_enrichment(session_factory, document_id))
        assert after_embed.embedding is not None
        assert after_embed.sentiment_label is None

        status, outcome = run(
            _run_stage_rerun(
                session_factory,
                monitor_id=monitor_id,
                stage=Stage.SENTIMENT,
                embedder=FixedEmbedder(),
                sentiment_scorer=SentimentScorer(),
                llm_router=router,
            )
        )

        after_sentiment = run(_load_enrichment(session_factory, document_id))
        assert after_sentiment.sentiment_label is not None
        assert after_sentiment.embedding == after_embed.embedding
        assert status is OverrideStatus.COMPLETE
        assert outcome["documents_reenriched"] == 1
        assert outcome["dates_recomputed"] == [date.today().isoformat()]

    def test_a_rerun_of_the_intent_stage_actually_classifies_through_the_router(self, session_factory) -> None:
        """The gap this covers: `enrich_documents` gates the intent stage on
        `llm_router is not None`, so a stage re-run built without a router
        would silently skip intent instead of re-running it."""
        monitor_id = run(_make_monitor(session_factory))
        source_id = run(_attach_play_source(session_factory, monitor_id))
        document_id = run(_seed_ledgered_document(session_factory, monitor_id, source_id))
        completion = Completion(
            value=IntentBatchResult(
                results=[IntentResult(document_id=str(document_id), intent="praise", confidence=0.9)]
            ),
            provider="fake", model="fake-model", prompt_version="v1",
        )
        provider = FakeProvider([completion])
        router = _make_router(provider)

        status, outcome = run(
            _run_stage_rerun(
                session_factory,
                monitor_id=monitor_id,
                stage=Stage.INTENT,
                embedder=FixedEmbedder(),
                sentiment_scorer=SentimentScorer(),
                llm_router=router,
            )
        )

        assert status is OverrideStatus.COMPLETE
        assert provider.calls == 1
        enrichment = run(_load_enrichment(session_factory, document_id))
        assert enrichment.intent == "praise"


class TestBackfillWindow:
    def test_finishes_partial_with_the_failed_date_listed(self, session_factory, monkeypatch) -> None:
        monitor_id = run(_make_monitor(session_factory))
        run(_attach_play_source(session_factory, monitor_id))

        calls = {"n": 0}

        async def fake_collect_one_source(session_factory, fetch_client, run_id, source):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                return SourceOutcome(
                    source_id=source.id, source_name=source.source_name.value,
                    result=SourceResult.COLLECTED, kept=3,
                )
            return SourceOutcome(
                source_id=source.id, source_name=source.source_name.value,
                result=SourceResult.FAILED_TRANSPORT, error="connection refused",
            )

        monkeypatch.setattr("mlsc.tasks.overrides.collect_one_source", fake_collect_one_source)

        status, outcome = run(
            _run_backfill_window(
                session_factory,
                fetch_client=None,
                monitor_id=monitor_id,
                window_start=date.today() - timedelta(days=2),
                window_end=date.today() - timedelta(days=1),
            )
        )

        assert status is OverrideStatus.PARTIAL
        assert outcome["documents_kept"] == 3
        assert len(outcome["dates_collected"]) == 1
        assert len(outcome["failures"]) == 1
        assert outcome["failures"][0]["error"] == "connection refused"
