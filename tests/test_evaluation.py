"""Core tests for the evaluation harness over a small fixture corpus. No
stochastic algorithm in the loop; each measure's arithmetic is asserted
against hand-computed values.

Requirements: 2, 3, 4, 5, 6, 8, 9.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any

import pytest
from sqlalchemy import pool, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from mlsc.application.labels import LabelContaminated, LabelIngestionService
from mlsc.application.monitors import MonitorService
from mlsc.config import TrendDetectionSettings
from mlsc.db.models import (
    Assignment,
    AssignmentMethod,
    Base,
    DetectionMethod,
    Document,
    Enrichment,
    EventKind,
    Purpose,
    Report,
    SourceName,
    TargetType,
    Topic,
    TrendEvent,
    TrendScore,
)
from mlsc.evaluation.corroboration import measure_corroboration
from mlsc.evaluation.detection import match_events
from mlsc.evaluation.harness import run_harness
from mlsc.evaluation.measures import MeasureStatus
from mlsc.evaluation.relevance import measure_relevance
from mlsc.pipeline.normalize import hash_author, hash_content
from mlsc.repositories.evaluation import DocumentLabelRepository
from mlsc.schemas.monitors import MonitorCreateRequest

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


async def _make_monitor(session_factory: async_sessionmaker) -> uuid.UUID:
    monitor = await MonitorService(session_factory).create(
        MonitorCreateRequest(
            name="Roblox", target_type=TargetType.PRODUCT, seed={"identifiers": ["x"]},
            cron_expression="0 3 * * *", timezone="UTC", retention_days=90,
        )
    )
    return monitor.id


async def _add_document(
    session_factory: async_sessionmaker, *, monitor_id: uuid.UUID, is_relevant: bool
) -> uuid.UUID:
    document_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Document(
                id=document_id, monitor_id=monitor_id, source_name=SourceName.PLAY,
                external_id=str(document_id), entity_id="x", author_hash=hash_author("u"),
                body="text", published_at=datetime.now(dt_timezone.utc),
                content_hash=hash_content(str(document_id)), raw={},
            )
        )
        await session.flush()
        session.add(
            Enrichment(
                id=uuid.uuid4(), document_id=document_id, is_relevant=is_relevant,
                embedding=[0.1] * 384, model_versions={},
            )
        )
        await session.commit()
    return document_id


_BASE = date(2026, 8, 1)


class TestRelevanceMeasure:
    def test_precision_and_recall_match_hand_computed_confusion_counts(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        # pipeline predicts relevant for 3, not relevant for 1; ground truth
        # labels all 4 as actually relevant -> 3 true positives, 1 false negative
        document_ids = [
            run(_add_document(session_factory, monitor_id=monitor_id, is_relevant=(i < 3)))
            for i in range(4)
        ]

        label_service = LabelIngestionService(session_factory)
        label_set_id = run(label_service.create_label_set(monitor_id, purpose=Purpose.RELEVANCE, notes=None))
        for document_id in document_ids:
            run(label_service.add_document_label(label_set_id, document_id=document_id, is_relevant=True, labelled_by="alice"))

        async def load():
            async with session_factory() as session:
                labels = await DocumentLabelRepository(session).for_label_set(label_set_id)
                return await measure_relevance(session, labels=labels)

        precision, recall = run(load())
        assert precision.value == 1.0  # no false positives
        assert recall.value == 0.75  # 3 of 4 actually-relevant documents predicted relevant

    def test_no_labels_reports_unavailable_not_zero(self, session_factory: async_sessionmaker) -> None:
        async def load():
            async with session_factory() as session:
                return await measure_relevance(session, labels=[])

        precision, recall = run(load())
        assert precision.status is MeasureStatus.UNAVAILABLE_NO_LABELS
        assert precision.value is None  # unavailable, not a computed zero
        assert recall.status is MeasureStatus.UNAVAILABLE_NO_LABELS


class TestLabelContamination:
    def test_a_label_named_after_a_system_component_is_rejected(self, session_factory: async_sessionmaker) -> None:
        monitor_id = run(_make_monitor(session_factory))
        document_id = run(_add_document(session_factory, monitor_id=monitor_id, is_relevant=True))
        service = LabelIngestionService(session_factory)
        label_set_id = run(service.create_label_set(monitor_id, purpose=Purpose.RELEVANCE, notes=None))

        with pytest.raises(LabelContaminated):
            run(service.add_document_label(label_set_id, document_id=document_id, is_relevant=True, labelled_by="auto-pipeline"))

    def test_an_event_label_referencing_the_systems_own_output_is_rejected(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        service = LabelIngestionService(session_factory)
        label_set_id = run(service.create_label_set(monitor_id, purpose=Purpose.EVENTS, notes=None))

        with pytest.raises(LabelContaminated):
            run(
                service.add_event_label(
                    label_set_id, monitor_id=monitor_id, occurred_on=_BASE, kind="burst",
                    description="x", external_reference="https://internal.example.com/monitors/x/trend_events/1",
                )
            )

    def test_an_event_label_with_no_external_reference_is_rejected(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        service = LabelIngestionService(session_factory)
        label_set_id = run(service.create_label_set(monitor_id, purpose=Purpose.EVENTS, notes=None))

        with pytest.raises(LabelContaminated):
            run(
                service.add_event_label(
                    label_set_id, monitor_id=monitor_id, occurred_on=_BASE, kind="burst",
                    description="x", external_reference="",
                )
            )


class TestEventMatching:
    def test_a_detected_event_within_tolerance_matches_with_correct_lead_time(self) -> None:
        from mlsc.db.models import EventLabel

        monitor_id = uuid.uuid4()
        topic_id = uuid.uuid4()
        early_event = TrendEvent(
            id=uuid.uuid4(), monitor_id=monitor_id, topic_id=topic_id, detected_on=_BASE + timedelta(days=3),
            kind=EventKind.BURST, method=DetectionMethod.ROBUST_Z, severity=1.0, statistics={}, evidence_ids=[],
        )
        unrelated_event = TrendEvent(
            id=uuid.uuid4(), monitor_id=monitor_id, topic_id=topic_id, detected_on=_BASE + timedelta(days=20),
            kind=EventKind.BURST, method=DetectionMethod.ROBUST_Z, severity=1.0, statistics={}, evidence_ids=[],
        )
        matched_label = EventLabel(
            id=uuid.uuid4(), label_set_id=uuid.uuid4(), monitor_id=monitor_id,
            occurred_on=_BASE + timedelta(days=5), kind="outage", description="x",
            external_reference="https://news.example.com/1",
        )
        unmatched_label = EventLabel(
            id=uuid.uuid4(), label_set_id=uuid.uuid4(), monitor_id=monitor_id,
            occurred_on=_BASE + timedelta(days=10), kind="outage", description="x",
            external_reference="https://news.example.com/2",
        )

        matches = match_events([early_event, unrelated_event], [matched_label, unmatched_label], tolerance_days=3)

        assert len(matches) == 1
        assert matches[0].matched_event_id == early_event.id
        assert matches[0].lead_time_days == 2  # label at day 5, event fired at day 3


class TestCorroboration:
    def test_corroborated_events_hit_more_often_than_single_source_events(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        corroborated_topic_id = uuid.uuid4()
        single_source_topic_id = uuid.uuid4()

        async def seed():
            async with session_factory() as session:
                session.add(Topic(id=corroborated_topic_id, monitor_id=monitor_id, label="a", keywords=[], centroid=[0.1] * 384, doc_count=0, first_seen=_BASE, last_seen=_BASE))
                session.add(Topic(id=single_source_topic_id, monitor_id=monitor_id, label="b", keywords=[], centroid=[0.2] * 384, doc_count=0, first_seen=_BASE, last_seen=_BASE))
                await session.flush()

                session.add(TrendEvent(id=uuid.uuid4(), monitor_id=monitor_id, topic_id=corroborated_topic_id, detected_on=_BASE, kind=EventKind.BURST, method=DetectionMethod.ROBUST_Z, severity=1.0, statistics={}, evidence_ids=[]))
                session.add(TrendScore(id=uuid.uuid4(), monitor_id=monitor_id, topic_id=corroborated_topic_id, bucket=_BASE, value=0.5, components={"breadth_ratio": 0.8}, penalties={}))

                session.add(TrendEvent(id=uuid.uuid4(), monitor_id=monitor_id, topic_id=single_source_topic_id, detected_on=_BASE + timedelta(days=10), kind=EventKind.BURST, method=DetectionMethod.ROBUST_Z, severity=1.0, statistics={}, evidence_ids=[]))
                session.add(TrendScore(id=uuid.uuid4(), monitor_id=monitor_id, topic_id=single_source_topic_id, bucket=_BASE + timedelta(days=10), value=0.5, components={"breadth_ratio": 0.2}, penalties={}))
                await session.commit()

        run(seed())

        from mlsc.db.models import EventLabel

        labels = [
            EventLabel(id=uuid.uuid4(), label_set_id=uuid.uuid4(), monitor_id=monitor_id, occurred_on=_BASE, kind="x", description="x", external_reference="https://news.example.com/1"),
        ]

        async def load():
            async with session_factory() as session:
                return await measure_corroboration(session, monitor_id=monitor_id, labels=labels)

        corroborated_rate, single_source_rate = run(load())
        assert corroborated_rate.value == 1.0
        assert single_source_rate.value == 0.0


class TestHarness:
    def test_two_runs_over_the_same_period_and_configuration_produce_identical_numbers(
        self, session_factory: async_sessionmaker
    ) -> None:
        """Requirement 9."""
        monitor_id = run(_make_monitor(session_factory))
        document_id = run(_add_document(session_factory, monitor_id=monitor_id, is_relevant=True))

        label_service = LabelIngestionService(session_factory)
        label_set_id = run(label_service.create_label_set(monitor_id, purpose=Purpose.RELEVANCE, notes=None))
        run(label_service.add_document_label(label_set_id, document_id=document_id, is_relevant=True, labelled_by="alice"))

        settings = TrendDetectionSettings()
        report_id_1 = run(
            run_harness(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_BASE + timedelta(days=5),
                trend_settings=settings,
            )
        )
        report_id_2 = run(
            run_harness(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_BASE + timedelta(days=5),
                trend_settings=settings,
            )
        )

        async def load(report_id: uuid.UUID) -> Report:
            async with session_factory() as session:
                return await session.get(Report, report_id)

        report_1 = run(load(report_id_1))
        report_2 = run(load(report_id_2))

        assert report_1.config_fingerprint == report_2.config_fingerprint
        assert report_1.measures["relevance"] == report_2.measures["relevance"]

    def test_a_missing_label_set_reports_unavailable_and_does_not_crash_the_run(
        self, session_factory: async_sessionmaker
    ) -> None:
        monitor_id = run(_make_monitor(session_factory))
        settings = TrendDetectionSettings()

        report_id = run(
            run_harness(
                session_factory, monitor_id=monitor_id, period_start=_BASE, period_end=_BASE + timedelta(days=5),
                trend_settings=settings,
            )
        )

        async def load() -> Report:
            async with session_factory() as session:
                return await session.get(Report, report_id)

        report = run(load())
        relevance = report.measures["relevance"]
        assert relevance[0]["status"] == "unavailable_no_labels"
        assert "error" not in report.measures  # no measure crashed the run
