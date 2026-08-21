"""The shared shapes every detector produces, and the interface the pipeline
calls them through.

``TestResult`` and ``Candidate`` are separate types on purpose (design.md,
"Domain shapes"): a detector only ever produces the former, and only the
pipeline — after gates and correction — is allowed to promote one into the
latter. A detector that constructed a ``Candidate`` itself could bypass
correction by construction, which is the one thing this design forbids.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Protocol

from mlsc.db.models import DetectionMethod, Direction, EventKind
from mlsc.pipeline.analytics.gates import Baseline
from mlsc.pipeline.analytics.series import Series


@dataclasses.dataclass(frozen=True)
class TestResult:
    method: DetectionMethod
    statistic: float
    p_value: float
    observed: float
    expected: float
    direction: Direction


@dataclasses.dataclass(frozen=True)
class Candidate:
    topic_id: uuid.UUID
    kind: EventKind
    method: DetectionMethod
    test_result: TestResult


class Detector(Protocol):
    """One statistical test over one topic's deseasonalised series.

    Returns ``None`` when the series is unsuited to this method (too short,
    all-zero, or otherwise degenerate) rather than raising — the caller
    treats any exception as ``DetectorFailed`` and continues with the rest
    of the ensemble regardless, but a well-behaved detector should not rely
    on that path for its ordinary "nothing to say" case.
    """

    def test(self, series: Series, baseline: Baseline) -> TestResult | None: ...
