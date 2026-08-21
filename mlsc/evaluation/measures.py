"""``Measure`` carries a status alongside its value (design.md, "Domain
shapes") — requirement 8's mechanism: a measure with no labels is
``unavailable`` with a reason, never absent and never a zero standing in
for "not computed" (learn.md-equivalent: the same failure C5 forbids for a
missing source).
"""

from __future__ import annotations

import dataclasses

from mlsc.db.models import MeasureStatus


@dataclasses.dataclass(frozen=True)
class Measure:
    name: str
    value: float | None
    sample_size: int
    computed_over: str
    status: MeasureStatus


def computed(name: str, value: float, *, sample_size: int, computed_over: str) -> Measure:
    return Measure(name=name, value=value, sample_size=sample_size, computed_over=computed_over, status=MeasureStatus.COMPUTED)


def unavailable_no_labels(name: str, *, computed_over: str) -> Measure:
    return Measure(
        name=name, value=None, sample_size=0, computed_over=computed_over,
        status=MeasureStatus.UNAVAILABLE_NO_LABELS,
    )


def unavailable_insufficient_history(name: str, *, sample_size: int, computed_over: str) -> Measure:
    return Measure(
        name=name, value=None, sample_size=sample_size, computed_over=computed_over,
        status=MeasureStatus.UNAVAILABLE_INSUFFICIENT_HISTORY,
    )
