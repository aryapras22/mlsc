"""Kleinberg-style burst state detection: a topic enters an elevated state
when its count exceeds a scaled multiple of the baseline rate, and the
severity is how far above that state it climbed while inside it.

The simplification kept from Kleinberg's original automaton: instead of an
infinite ladder of states with transition costs, this uses exactly two
states — normal and bursting — with a scale factor separating them. That is
enough to answer "is today (or the last few days) a burst", which is what
requirement 3 asks a burst detector for, without the machinery a multi-level
state ladder needs for a use case this pipeline does not have (ranking
burst intensity across many simultaneous bursts).
"""

from __future__ import annotations

from scipy import stats

from mlsc.db.models import DetectionMethod, Direction
from mlsc.pipeline.analytics.detectors.base import TestResult
from mlsc.pipeline.analytics.gates import Baseline
from mlsc.pipeline.analytics.series import PointQuality, Series

_BURST_LOOKBACK_DAYS = 3


def test(series: Series, baseline: Baseline, *, scale_factor: float) -> TestResult | None:
    if baseline.lambda_ <= 0:
        return None

    burst_threshold = baseline.lambda_ * scale_factor
    recent = [
        point for point in series.points[-_BURST_LOOKBACK_DAYS:]
        if point.quality is PointQuality.CLEAN and point.value is not None
    ]
    if not recent:
        return None

    bursting = [point for point in recent if point.value >= burst_threshold]
    if not bursting:
        return None

    peak = max(point.value for point in bursting)
    intensity = peak / baseline.lambda_

    # The Kleinberg state assignment decides *whether* today counts as
    # bursting; the p-value that decides whether this candidate survives
    # correction still needs to be an honest tail probability, or this
    # detector would bypass requirement 6 by construction.
    p_value = float(stats.poisson.sf(peak - 1, baseline.lambda_))

    return TestResult(
        method=DetectionMethod.KLEINBERG,
        statistic=intensity,
        p_value=p_value,
        observed=peak,
        expected=baseline.lambda_,
        direction=Direction.RISING,
    )
