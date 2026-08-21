"""The robust baseline and the gates that decide whether a topic is even
eligible for testing on a given day.

Median and MAD (median absolute deviation) rather than mean and standard
deviation: one spike poisons a mean for a month, but the median barely
moves and the MAD only a little (design.md, "Alternatives"). ``lambda`` is
the day's own mean rate, used by the sparse-count detector rather than the
robust one, since a topic with median zero has no meaningful MAD.

Every gate returns a ``GateOutcome`` rather than raising — a new topic
below the volume floor, or one without enough clean history, is the
ordinary state of the product, and requirement 4 asks that the reason be
recorded, not that the pipeline stop (design.md, "Failure strategy").
"""

from __future__ import annotations

import dataclasses
import statistics
import uuid
from datetime import date

from mlsc.db.models import GateReason
from mlsc.pipeline.analytics.series import PointQuality, Series

_MAD_TO_STD = 1.4826  # scales MAD to be comparable to a standard deviation under normality


@dataclasses.dataclass(frozen=True)
class Baseline:
    median: float
    mad: float
    lambda_: float
    clean_days: int
    window: int


@dataclasses.dataclass(frozen=True)
class GateOutcome:
    topic_id: uuid.UUID
    bucket: date
    passed: bool
    reason: GateReason | None = None


class BaselineUnusable(RuntimeError):
    """Too few clean days after quality filtering (design.md, "Named
    failures"). Raised only so the gate calling it can turn it into a
    recorded outcome — it never escapes this module."""


def volume_floor(series: Series, bucket: date, *, min_volume_floor: int) -> GateOutcome:
    """Requirement 4: a topic whose most recent clean volume sits below the
    project's floor is not tested at all — there is nothing a test could
    responsibly call a change against so little activity."""
    latest = _latest_point(series, bucket)
    if latest is None or latest.value is None or latest.value < min_volume_floor:
        return GateOutcome(topic_id=series.topic_id, bucket=bucket, passed=False, reason=GateReason.BELOW_VOLUME_FLOOR)
    return GateOutcome(topic_id=series.topic_id, bucket=bucket, passed=True)


def estimate_baseline(series: Series, bucket: date, *, min_clean_baseline_days: int) -> Baseline:
    """Requirement 5: only clean days enter the baseline. Raises
    ``BaselineUnusable`` when too few remain — the caller turns that into a
    ``GateOutcome`` rather than letting it propagate."""
    history = [point for point in series.points if point.bucket < bucket]
    clean = [point for point in history if point.quality is PointQuality.CLEAN and point.value is not None]

    if len(clean) < min_clean_baseline_days:
        raise BaselineUnusable(
            f"{len(clean)} clean days available, {min_clean_baseline_days} required"
        )

    values = [point.value for point in clean]
    median = statistics.median(values)
    mad = statistics.median([abs(value - median) for value in values]) * _MAD_TO_STD
    lambda_ = statistics.fmean(values)

    return Baseline(median=median, mad=mad, lambda_=lambda_, clean_days=len(clean), window=len(history))


def baseline_sufficient(
    series: Series, bucket: date, *, min_clean_baseline_days: int
) -> tuple[GateOutcome, Baseline | None]:
    """Wraps ``estimate_baseline`` so the pipeline gets one ``GateOutcome``
    either way, per design.md's success path."""
    try:
        baseline = estimate_baseline(series, bucket, min_clean_baseline_days=min_clean_baseline_days)
    except BaselineUnusable:
        return (
            GateOutcome(
                topic_id=series.topic_id, bucket=bucket, passed=False,
                reason=GateReason.INSUFFICIENT_BASELINE,
            ),
            None,
        )
    return GateOutcome(topic_id=series.topic_id, bucket=bucket, passed=True), baseline


def cooldown(
    topic_id: uuid.UUID, bucket: date, kind: object, *, last_event_dates: dict[object, date], cooldown_days: int
) -> GateOutcome:
    """Requirement 7: a repeat of the same kind within its cooldown window
    is dropped, checked after correction so a candidate corrected away
    never consumes a window it did not earn (design.md, "Success path")."""
    last_date = last_event_dates.get(kind)
    if last_date is not None and (bucket - last_date).days < cooldown_days:
        return GateOutcome(topic_id=topic_id, bucket=bucket, passed=False, reason=GateReason.COOLDOWN_ACTIVE)
    return GateOutcome(topic_id=topic_id, bucket=bucket, passed=True)


def _latest_point(series: Series, bucket: date):
    for point in reversed(series.points):
        if point.bucket == bucket:
            return point
    return None
