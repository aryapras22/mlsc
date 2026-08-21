"""Mann-Kendall trend test: is the recent window rising or falling, once
weekly rhythm has been removed.

A point-anomaly detector answers "is today unusual"; this answers "is the
recent window going somewhere" — the distinction requirement 3 draws
between a spike and sustained growth or decline. Sen's slope gives the
magnitude a severity can be built from.
"""

from __future__ import annotations

import pymannkendall as mk

from mlsc.db.models import DetectionMethod, Direction
from mlsc.pipeline.analytics.detectors.base import TestResult
from mlsc.pipeline.analytics.gates import Baseline
from mlsc.pipeline.analytics.series import PointQuality, Series

_MIN_WINDOW_POINTS = 8


def test(series: Series, baseline: Baseline, *, window_days: int, alpha: float) -> TestResult | None:
    recent = [
        point for point in series.points[-window_days:]
        if point.quality is PointQuality.CLEAN and point.value is not None
    ]
    if len(recent) < _MIN_WINDOW_POINTS:
        return None

    values = [point.value for point in recent]
    result = mk.original_test(values, alpha=alpha)
    if not result.h:
        return None

    return TestResult(
        method=DetectionMethod.MANN_KENDALL,
        statistic=float(result.z),
        p_value=float(result.p),
        observed=float(result.slope),
        expected=0.0,
        direction=Direction.RISING if result.trend == "increasing" else Direction.FALLING,
    )
