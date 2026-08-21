"""The normalisation comparison: rebuilding a topic's series under each
candidate normalisation strategy and replaying production detection over
each rebuild, so the comparison measures the actual detector code rather
than a second implementation of it (design.md, "Dependencies, injected":
"a harness with its own copy of the detection pipeline reports numbers
about code nobody runs").

Series stability here is the coefficient of variation of an arm's clean
values — a strategy sensitive to sample-size churn rather than real
change produces a noisier series and a higher coefficient, which is
exactly what requirement 5 asks this comparison to expose (§handoff D7).
"""

from __future__ import annotations

import statistics
import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.db.models import DailyMetric, EventLabel
from mlsc.evaluation.detection import match_events
from mlsc.evaluation.measures import Measure, computed, unavailable_no_labels
from mlsc.pipeline.analytics.detectors import robust_z
from mlsc.pipeline.analytics.gates import estimate_baseline, BaselineUnusable
from mlsc.pipeline.analytics.series import PointQuality, Series, SeriesPoint

_MIN_CLEAN_BASELINE_DAYS = 14


async def rebuild_series(
    session: AsyncSession, *, monitor_id: uuid.UUID, topic_id: uuid.UUID,
    window_dates: list[date], arm: str,
) -> Series:
    """Requirement 5: the same window rebuilt with a different value column
    selected per arm. ``"raw_count"`` reads ``doc_count`` directly;
    ``"prevalence"`` reads ``doc_count_share`` — the same rows, a different
    column, so the only variable between arms is the normalisation itself.
    """
    result = await session.execute(
        select(DailyMetric).where(
            DailyMetric.monitor_id == monitor_id, DailyMetric.topic_id == topic_id,
            DailyMetric.source_name.is_(None), DailyMetric.bucket.in_(window_dates),
        )
    )
    metrics_by_bucket = {row.bucket: row for row in result.scalars().all()}

    points: list[SeriesPoint] = []
    for bucket in window_dates:
        metric = metrics_by_bucket.get(bucket)
        if metric is None:
            points.append(SeriesPoint(bucket=bucket, value=None, sample_size=0, quality=PointQuality.ABSENT))
            continue
        value = metric.doc_count if arm == "raw_count" else metric.doc_count_share
        quality = PointQuality.TRUNCATED if metric.quota_hit else PointQuality.CLEAN
        points.append(SeriesPoint(bucket=bucket, value=value, sample_size=metric.sample_size, quality=quality))

    return Series(topic_id=topic_id, points=points)


def _detect_bursts(series: Series, *, z_threshold: float) -> list[date]:
    """Replays the production robust-z detector day by day over a rebuilt
    series — the injected detector, not a reimplementation."""
    flagged: list[date] = []
    for index, point in enumerate(series.points):
        if point.quality is not PointQuality.CLEAN or point.value is None:
            continue
        window = Series(topic_id=series.topic_id, points=series.points[: index + 1])
        try:
            baseline = estimate_baseline(window, point.bucket, min_clean_baseline_days=_MIN_CLEAN_BASELINE_DAYS)
        except BaselineUnusable:
            continue
        result = robust_z.test(window, baseline, z_threshold=z_threshold)
        if result is not None:
            flagged.append(point.bucket)
    return flagged


def _stability(series: Series) -> float:
    """Coefficient of variation over clean values — lower means less noise
    for a strategy sensitive to artefacts, not real change, to hide in."""
    clean_values = [point.value for point in series.points if point.quality is PointQuality.CLEAN and point.value is not None]
    if len(clean_values) < 2:
        return 0.0
    mean = statistics.fmean(clean_values)
    if mean == 0:
        return 0.0
    return statistics.pstdev(clean_values) / mean


async def measure_normalisation(
    session: AsyncSession, *, monitor_id: uuid.UUID, topic_id: uuid.UUID,
    window_dates: list[date], labels: list[EventLabel], z_threshold: float,
) -> dict[str, tuple[Measure, Measure]]:
    """Requirement 5: for ``raw_count`` and ``prevalence``, rebuild the
    series and replay detection, then report series stability and the
    false-positive rate against the same label set. ``external_index`` has
    no built source yet, so it is reported unavailable rather than the
    comparison silently having two arms and claiming three (design.md,
    "Domain shapes": ``NormalisationArm``)."""
    results: dict[str, tuple[Measure, Measure]] = {}

    for arm in ("raw_count", "prevalence"):
        series = await rebuild_series(session, monitor_id=monitor_id, topic_id=topic_id, window_dates=window_dates, arm=arm)
        flagged_dates = _detect_bursts(series, z_threshold=z_threshold)

        stability = computed(
            f"normalisation_stability_{arm}", _stability(series), sample_size=len(series.points),
            computed_over=f"{len(window_dates)}-day window",
        )

        if not labels:
            false_positive_rate = unavailable_no_labels(f"normalisation_false_positive_rate_{arm}", computed_over="0 event labels")
        else:
            synthetic_events = _as_trend_events(flagged_dates, monitor_id=monitor_id, topic_id=topic_id)
            matches = match_events(synthetic_events, labels, tolerance_days=3)
            false_positive_count = len(synthetic_events) - len(matches)
            rate = false_positive_count / len(synthetic_events) if synthetic_events else 0.0
            false_positive_rate = computed(
                f"normalisation_false_positive_rate_{arm}", rate, sample_size=len(synthetic_events),
                computed_over=f"{len(synthetic_events)} flagged days against {len(labels)} labels",
            )

        results[arm] = (stability, false_positive_rate)

    results["external_index"] = (
        unavailable_no_labels("normalisation_stability_external_index", computed_over="no external-index source is built yet"),
        unavailable_no_labels("normalisation_false_positive_rate_external_index", computed_over="no external-index source is built yet"),
    )
    return results


def _as_trend_events(flagged_dates: list[date], *, monitor_id: uuid.UUID, topic_id: uuid.UUID) -> list:
    from mlsc.db.models import DetectionMethod, EventKind, TrendEvent

    return [
        TrendEvent(
            id=uuid.uuid4(), monitor_id=monitor_id, topic_id=topic_id, detected_on=flagged_date,
            kind=EventKind.BURST, method=DetectionMethod.ROBUST_Z, severity=0.0, statistics={}, evidence_ids=[],
        )
        for flagged_date in flagged_dates
    ]
