"""The harness task: runs every measure over one monitor and period, and
writes one report.

Every measure node returns a value rather than raising for missing inputs
(design.md, "Success path") — the one exception is a single measure
raising unexpectedly, which is recorded as failed rather than aborting the
run, so a report is more useful partially computed than not computed at
all (design.md, "Failure strategy").
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import subprocess
import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mlsc.config import TrendDetectionSettings
from mlsc.db.models import Purpose, Report, Topic
from mlsc.evaluation.corroboration import measure_corroboration
from mlsc.evaluation.detection import measure_detection
from mlsc.evaluation.generation import Embedder, measure_generation
from mlsc.evaluation.measures import Measure
from mlsc.evaluation.normalization import measure_normalisation
from mlsc.evaluation.relevance import measure_relevance
from mlsc.evaluation.topics import measure_coherence, measure_diversity, measure_stability
from mlsc.llm.router import LlmRouter
from mlsc.pipeline.insights.context import ContextEmpty, assemble as assemble_context
from mlsc.repositories.evaluation import DocumentLabelRepository, EventLabelRepository, LabelSetRepository, ReportRepository

logger = logging.getLogger(__name__)

_STABILITY_LOOKBACK_SNAPSHOTS = 4


async def run_harness(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    monitor_id: uuid.UUID,
    period_start: date,
    period_end: date,
    trend_settings: TrendDetectionSettings,
    generation_arms: dict[str, LlmRouter] | None = None,
    embedder: Embedder | None = None,
) -> uuid.UUID:
    """Requirement 1: one report per run, capturing which configuration and
    code version produced it. Completes every measure it can even when one
    fails (design.md, "Failure strategy": "A single measure raising
    unexpectedly")."""
    measures: dict[str, dict] = {}

    async with session_factory() as session:
        relevance_labels = await _relevance_labels(session, monitor_id)
        event_labels = await _event_labels(session, monitor_id)

        _record(measures, "relevance", lambda: measure_relevance(session, labels=relevance_labels))
        _record(measures, "coherence", lambda: measure_coherence(session, monitor_id=monitor_id))
        _record(measures, "diversity", lambda: measure_diversity(session, monitor_id=monitor_id))
        _record(measures, "stability", lambda: measure_stability(session, monitor_id=monitor_id, lookback=_STABILITY_LOOKBACK_SNAPSHOTS))
        _record(measures, "detection", lambda: measure_detection(session, monitor_id=monitor_id, labels=event_labels))
        _record(measures, "corroboration", lambda: measure_corroboration(session, monitor_id=monitor_id, labels=event_labels))

        topics = list((await session.execute(_active_topics_query(monitor_id))).scalars().all())
        for topic in topics:
            window_dates = _window_dates(period_start, period_end)
            _record(
                measures, f"normalisation_{topic.id}",
                lambda topic_id=topic.id: measure_normalisation(
                    session, monitor_id=monitor_id, topic_id=topic_id, window_dates=window_dates,
                    labels=event_labels, z_threshold=trend_settings.burst_z_threshold,
                ),
            )

        if generation_arms:
            _record(
                measures, "generation",
                lambda: _run_generation(session, topics, period_start, period_end, generation_arms, embedder),
            )

        measures = await _await_pending(measures)

        report = Report(
            id=uuid.uuid4(), monitor_id=monitor_id, period_start=period_start, period_end=period_end,
            measures=_serialise(measures), config_fingerprint=_fingerprint(trend_settings),
            code_version=_code_version(),
        )
        ReportRepository(session).insert(report)
        await session.commit()
        return report.id


def _record(measures: dict, key: str, coroutine_factory) -> None:
    """Stores the coroutine for later awaiting rather than awaiting inline,
    so a raise from one measure is caught per-key without unwinding the
    whole function (design.md, "Failure strategy")."""
    measures[key] = coroutine_factory()


async def _await_pending(pending: dict) -> dict:
    resolved: dict = {}
    for key, coroutine in pending.items():
        try:
            resolved[key] = await coroutine
        except Exception:  # noqa: BLE001 - one measure failing must not abort the others
            logger.exception("measure %s failed to compute", key)
            resolved[key] = {"error": "measure_failed"}
    return resolved


async def _run_generation(
    session: AsyncSession, topics: list[Topic], period_start: date, period_end: date,
    generation_arms: dict[str, LlmRouter], embedder: Embedder | None,
) -> dict:
    """Requirement 7: assembles each active topic's real ``TopicContext``
    — the same value the production generation task builds — and scores
    every configured arm against it. A topic with no representatives in
    the period is skipped rather than aborting the whole comparison."""
    if embedder is None:
        raise ValueError("embedder is required when generation_arms is provided")

    contexts = []
    for topic in topics:
        try:
            contexts.append(await assemble_context(session, topic=topic, period_start=period_start, period_end=period_end))
        except ContextEmpty:
            continue

    if not contexts:
        return {}
    return await measure_generation(contexts, arms=generation_arms, embedder=embedder)


async def _relevance_labels(session: AsyncSession, monitor_id: uuid.UUID) -> list:
    label_set = await LabelSetRepository(session).latest_for(monitor_id, purpose=Purpose.RELEVANCE)
    if label_set is None:
        return []
    return await DocumentLabelRepository(session).for_label_set(label_set.id)


async def _event_labels(session: AsyncSession, monitor_id: uuid.UUID) -> list:
    label_set = await LabelSetRepository(session).latest_for(monitor_id, purpose=Purpose.EVENTS)
    if label_set is None:
        return []
    return await EventLabelRepository(session).for_label_set(label_set.id)


def _active_topics_query(monitor_id: uuid.UUID):
    from mlsc.db.models import TopicStatus
    from sqlalchemy import select

    return select(Topic).where(Topic.monitor_id == monitor_id, Topic.status == TopicStatus.ACTIVE)


def _window_dates(period_start: date, period_end: date) -> list[date]:
    from datetime import timedelta

    days = (period_end - period_start).days
    return [period_start + timedelta(days=offset) for offset in range(days + 1)]


def _serialise(measures: dict) -> dict:
    """Recursively converts every ``Measure`` in the nested result
    structure into a plain dict, so the whole tree is JSON-serialisable for
    the report's JSONB column."""
    def convert(value):
        if isinstance(value, Measure):
            return dataclasses.asdict(value) | {"status": value.status.value}
        if isinstance(value, dict):
            return {str(k): convert(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(v) for v in value]
        return value

    return convert(measures)


def _fingerprint(settings: TrendDetectionSettings) -> str:
    payload = json.dumps(settings.model_dump(), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _code_version() -> str:
    """Best-effort git commit hash — never load-bearing for the run itself,
    matching how a library's own version is read elsewhere in this codebase
    (design.md's provenance columns)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=True,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unknown"
