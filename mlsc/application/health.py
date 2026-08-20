"""The verdict rule: streak counters and the 28-day median decide source state.

Fewer runs than the median window needs returns unknown and raises nothing —
an alert firing during setup trains the operator to ignore alerts
(design.md, "Failure strategy").
"""

from __future__ import annotations

import dataclasses

from mlsc.db.models import SourceState

_EMPTY_STREAK_THRESHOLD = 2
_VOLUME_COLLAPSE_RATIO = 0.5
_MIN_HISTORY_FOR_MEDIAN = 5


@dataclasses.dataclass(frozen=True)
class HealthVerdict:
    state: SourceState
    reason: str | None = None


def evaluate(
    *,
    kept: int,
    previously_had_rows: bool,
    consecutive_empty: int,
    rows_median_28d: float | None,
    history_count: int,
) -> HealthVerdict:
    """Compute this run's verdict. Does not persist counters; the caller updates them."""
    if history_count < _MIN_HISTORY_FOR_MEDIAN:
        return HealthVerdict(SourceState.HEALTHY, reason="insufficient history")

    if kept == 0 and previously_had_rows and consecutive_empty + 1 >= _EMPTY_STREAK_THRESHOLD:
        return HealthVerdict(SourceState.BROKEN, reason="empty_streak")

    if rows_median_28d is not None and rows_median_28d > 0 and kept < rows_median_28d * _VOLUME_COLLAPSE_RATIO:
        return HealthVerdict(SourceState.DEGRADED, reason="volume_collapse")

    return HealthVerdict(SourceState.HEALTHY)
