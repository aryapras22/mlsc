"""Series construction: a topic's history of daily figures, each point
carrying the quality that decides whether it may anchor a baseline or
trigger a test.

``PointQuality`` travels on every point rather than being filtered out
before this layer, because a baseline built by silently dropping bad days
would forget why it is shorter than the window it was asked for (design.md,
"Domain shapes": "so a baseline cannot include truncated days by
forgetting to filter").
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date
from enum import Enum

from mlsc.db.models import DailyMetric, RunStatus


class PointQuality(str, Enum):
    CLEAN = "clean"
    TRUNCATED = "truncated"
    PARTIAL = "partial"
    ABSENT = "absent"


@dataclasses.dataclass(frozen=True)
class SeriesPoint:
    bucket: date
    value: float | None
    sample_size: int
    quality: PointQuality
    sentiment_mean: float | None = None
    negativity_rate: float | None = None


@dataclasses.dataclass(frozen=True)
class Series:
    topic_id: uuid.UUID
    points: list[SeriesPoint]

    def clean_points(self) -> list[SeriesPoint]:
        """Requirement 5: a truncated or partial day never anchors a
        baseline or is itself tested."""
        return [point for point in self.points if point.quality is PointQuality.CLEAN]


def build_series(
    topic_id: uuid.UUID,
    window_dates: list[date],
    metrics_by_bucket: dict[date, DailyMetric],
    run_status_by_bucket: dict[date, RunStatus],
) -> Series:
    """One topic's series over ``window_dates``, in order, with no gaps: a
    date with no metric row becomes an ``ABSENT`` point rather than being
    skipped, so a detector sees the topic's actual silence instead of a
    shortened series that looks the same as one starting later.
    """
    points: list[SeriesPoint] = []
    for bucket in window_dates:
        metric = metrics_by_bucket.get(bucket)
        run_status = run_status_by_bucket.get(bucket)

        if metric is None:
            points.append(
                SeriesPoint(bucket=bucket, value=None, sample_size=0, quality=PointQuality.ABSENT)
            )
            continue

        if run_status is RunStatus.PARTIAL:
            quality = PointQuality.PARTIAL
        elif metric.quota_hit:
            quality = PointQuality.TRUNCATED
        else:
            quality = PointQuality.CLEAN

        points.append(
            SeriesPoint(
                bucket=bucket,
                value=float(metric.doc_count),
                sample_size=metric.sample_size,
                quality=quality,
                sentiment_mean=metric.sentiment_mean,
                negativity_rate=metric.negativity_rate,
            )
        )

    return Series(topic_id=topic_id, points=points)
