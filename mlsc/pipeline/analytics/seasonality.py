"""Removes weekly rhythm before a series is tested for direction.

Every product this pipeline watches has a real weekend pattern — reviews
dip on Saturdays, forum threads dip on Sundays — and a direction test run on
the raw series would report that rhythm as growth or decline every single
week (requirement 8). The correction is a per-weekday multiplicative
factor: each weekday's typical level relative to the series median, applied
in reverse to flatten it out.
"""

from __future__ import annotations

import dataclasses
import statistics

from mlsc.pipeline.analytics.series import PointQuality, Series, SeriesPoint

_DAYS_IN_WEEK = 7


@dataclasses.dataclass(frozen=True)
class DeseasonalisedSeries:
    series: Series
    weekday_factors: dict[int, float]


def remove_weekly(series: Series) -> DeseasonalisedSeries:
    """Requirement 8: divide each clean point by its weekday's typical
    factor, so a Tuesday is compared to other Tuesdays' usual level rather
    than to the series as a whole.

    Falls back to no adjustment (all factors 1.0) when there is too little
    history to estimate a factor per weekday reliably — an under-corrected
    series is safer than one adjusted from noise.
    """
    clean = [point for point in series.points if point.quality is PointQuality.CLEAN and point.value is not None]
    if len(clean) < _DAYS_IN_WEEK * 2:
        return DeseasonalisedSeries(series=series, weekday_factors=dict.fromkeys(range(_DAYS_IN_WEEK), 1.0))

    overall_median = statistics.median(point.value for point in clean)
    if overall_median == 0:
        return DeseasonalisedSeries(series=series, weekday_factors=dict.fromkeys(range(_DAYS_IN_WEEK), 1.0))

    by_weekday: dict[int, list[float]] = {weekday: [] for weekday in range(_DAYS_IN_WEEK)}
    for point in clean:
        by_weekday[point.bucket.weekday()].append(point.value)

    factors = {
        weekday: (statistics.median(values) / overall_median if values else 1.0)
        for weekday, values in by_weekday.items()
    }

    adjusted_points = [
        _adjust_point(point, factors.get(point.bucket.weekday(), 1.0)) for point in series.points
    ]
    return DeseasonalisedSeries(
        series=Series(topic_id=series.topic_id, points=adjusted_points), weekday_factors=factors
    )


def _adjust_point(point: SeriesPoint, factor: float) -> SeriesPoint:
    if point.value is None or factor <= 0:
        return point
    return dataclasses.replace(point, value=point.value / factor)
