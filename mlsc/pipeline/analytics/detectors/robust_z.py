"""Robust median z-score: today's deviation from the baseline median, in
MAD units, for topics with enough volume that the median is a meaningful
centre.

Point anomaly, not direction — this detector answers "is today unusual",
which is what a burst is. Growth and decline are a different question,
answered by the Mann-Kendall detector over the whole recent window instead.
"""

from __future__ import annotations

from scipy import stats

from mlsc.db.models import DetectionMethod, Direction
from mlsc.pipeline.analytics.detectors.base import TestResult
from mlsc.pipeline.analytics.gates import Baseline
from mlsc.pipeline.analytics.series import Series


def test(series: Series, baseline: Baseline, *, z_threshold: float) -> TestResult | None:
    latest = series.points[-1] if series.points else None
    if latest is None or latest.value is None:
        return None
    if baseline.mad == 0:
        return None  # every clean day identical; no z-score is meaningful

    z_score = (latest.value - baseline.median) / baseline.mad
    if abs(z_score) < z_threshold:
        return None

    p_value = 2 * (1 - stats.norm.cdf(abs(z_score)))
    return TestResult(
        method=DetectionMethod.ROBUST_Z,
        statistic=z_score,
        p_value=p_value,
        observed=latest.value,
        expected=baseline.median,
        direction=Direction.RISING if z_score > 0 else Direction.FALLING,
    )
