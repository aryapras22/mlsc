"""Emergence: a topic appearing for the first time.

Not a test over a series — a topic on its first day has no history to test
against — so this looks at ``Topic.first_seen`` directly rather than at
``Series``. Kept alongside the other detectors because it produces the same
``TestResult`` shape the pipeline promotes into a candidate identically; it
simply is not offered ``Baseline.estimate``'s output as an input.
"""

from __future__ import annotations

from datetime import date

from mlsc.db.models import DetectionMethod, Direction
from mlsc.pipeline.analytics.detectors.base import TestResult


def test(first_seen: date, bucket: date, doc_count: int) -> TestResult | None:
    if first_seen != bucket:
        return None

    # Emergence has no meaningful p-value of its own — there is no baseline
    # a first day could be tested against — so this reuses poisson_exact,
    # the count-based method, as its label rather than inventing a seventh
    # method the design's closed ``Method`` set does not have room for.
    return TestResult(
        method=DetectionMethod.POISSON_EXACT,
        statistic=float(doc_count),
        p_value=0.0,
        observed=float(doc_count),
        expected=0.0,
        direction=Direction.RISING,
    )
