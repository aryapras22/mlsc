"""Exact Poisson test for sparse topics: whether today's count is unusual
against the baseline's own mean rate, when the counts are too small for a
median-based test to have any resolution (a median of 1 has no meaningful
spread).

An exact test rather than a normal approximation, because a sparse count
series is exactly where the approximation breaks down (requirement 2:
"a method appropriate to how sparse that topic's counts are").
"""

from __future__ import annotations

from scipy import stats

from mlsc.db.models import DetectionMethod, Direction
from mlsc.pipeline.analytics.detectors.base import TestResult
from mlsc.pipeline.analytics.gates import Baseline
from mlsc.pipeline.analytics.series import Series

_SPARSE_MEDIAN_CEILING = 5


def test(series: Series, baseline: Baseline, *, alpha: float) -> TestResult | None:
    if baseline.median > _SPARSE_MEDIAN_CEILING:
        return None  # dense enough for the robust z-score instead

    latest = series.points[-1] if series.points else None
    if latest is None or latest.value is None or baseline.lambda_ <= 0:
        return None

    observed = int(latest.value)
    expected = baseline.lambda_

    if observed >= expected:
        p_value = stats.poisson.sf(observed - 1, expected)
        direction = Direction.RISING
    else:
        p_value = stats.poisson.cdf(observed, expected)
        direction = Direction.FALLING

    if p_value >= alpha:
        return None

    return TestResult(
        method=DetectionMethod.POISSON_EXACT,
        statistic=float(observed),
        p_value=float(p_value),
        observed=float(observed),
        expected=expected,
        direction=direction,
    )
