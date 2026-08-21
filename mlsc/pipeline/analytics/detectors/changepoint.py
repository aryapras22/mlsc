"""Structural break: the single most likely point in the recent window where
the series' level shifted, tested for significance with a two-sample test
across the split.

Not a full multi-changepoint PELT search — `ruptures` has no build for the
pinned Python version (tasks.md, task 1's note) — but the same idea reduced
to the case this pipeline needs: one recent window, at most one break worth
reporting. Every candidate split is scored by the same sum-of-squares
reduction PELT itself optimises; only the significance test at the winning
split is simplified to a two-sample t-test.
"""

from __future__ import annotations

import statistics

from scipy import stats

from mlsc.db.models import DetectionMethod, Direction
from mlsc.pipeline.analytics.detectors.base import TestResult
from mlsc.pipeline.analytics.gates import Baseline
from mlsc.pipeline.analytics.series import PointQuality, Series

_MIN_SEGMENT_LENGTH = 4


def test(
    series: Series, baseline: Baseline, *, window_days: int, alpha: float, value_of=None
) -> TestResult | None:
    """``value_of`` extracts the quantity to test from a point — defaults to
    ``point.value`` (prevalence); pass ``lambda p: p.sentiment_mean`` for the
    sentiment-flip variant over the same window."""
    value_of = value_of or (lambda point: point.value)

    recent = [
        point for point in series.points[-window_days:]
        if point.quality is PointQuality.CLEAN and value_of(point) is not None
    ]
    if len(recent) < _MIN_SEGMENT_LENGTH * 2:
        return None

    values = [value_of(point) for point in recent]
    best_split, best_cost_reduction = _best_split(values)
    if best_split is None:
        return None

    before, after = values[:best_split], values[best_split:]
    statistic, raw_p_value = stats.ttest_ind(after, before, equal_var=False)

    # Searching every candidate split and keeping the best one is itself a
    # multiple-comparisons problem — the "look-elsewhere effect" — and
    # without correcting for it a flat, noisy series triggers this detector
    # almost every time simply because there are many splits to try.
    # Bonferroni over the number of splits searched keeps this detector's
    # own false-positive rate honest before the day-level correction ever
    # sees it.
    splits_tried = len(values) - 2 * _MIN_SEGMENT_LENGTH + 1
    p_value = min(1.0, raw_p_value * splits_tried)
    if p_value >= alpha:
        return None

    before_mean, after_mean = statistics.fmean(before), statistics.fmean(after)
    return TestResult(
        method=DetectionMethod.PELT,
        statistic=float(statistic),
        p_value=float(p_value),
        observed=after_mean,
        expected=before_mean,
        direction=Direction.RISING if after_mean > before_mean else Direction.FALLING,
    )


def _best_split(values: list[float]) -> tuple[int | None, float]:
    """The split point minimising the combined sum of squared deviations
    from each segment's own mean — the same objective a single-breakpoint
    PELT search optimises."""
    total_sse = _sse(values)
    best_index, best_reduction = None, 0.0

    for index in range(_MIN_SEGMENT_LENGTH, len(values) - _MIN_SEGMENT_LENGTH + 1):
        split_sse = _sse(values[:index]) + _sse(values[index:])
        reduction = total_sse - split_sse
        if reduction > best_reduction:
            best_index, best_reduction = index, reduction

    return best_index, best_reduction


def _sse(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    return sum((value - mean) ** 2 for value in values)
