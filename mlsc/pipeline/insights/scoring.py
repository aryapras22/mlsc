"""The evidence-derived RICE variant: Frequency, Severity, Momentum, Breadth,
Intent purity, Staleness — measurements standing in for a human's usual
guesses (learn.md, "RICE, and what an evidence-derived variant changes").

Effort is deliberately absent: the system knows nothing about the codebase,
so this score ranks demand, not cost. Storing every component beside the
total is what requirement 3 asks for and what makes recalibrating weights
later a recompute rather than a full regeneration.
"""

from __future__ import annotations

import dataclasses

from mlsc.pipeline.insights.context import TopicStatistics

_ACTIONABLE_INTENTS = ("feature_request", "bug_report")


@dataclasses.dataclass(frozen=True)
class ScoreWeights:
    frequency: float = 0.2
    severity: float = 0.25
    momentum: float = 0.2
    breadth: float = 0.15
    intent_purity: float = 0.15
    staleness: float = 0.05


@dataclasses.dataclass(frozen=True)
class OpportunityScoreComponents:
    frequency: float
    severity: float
    momentum: float
    breadth: float
    intent_purity: float
    staleness: float


@dataclasses.dataclass(frozen=True)
class OpportunityScore:
    value: float
    components: dict[str, float]


def score(statistics: TopicStatistics, *, days_since_last_mention: int, weights: ScoreWeights) -> OpportunityScore:
    """Every component in ``[0, 1]`` before weighting, so the total is
    comparable in the same way `trend-detection`'s within-day normalisation
    is — this score compares opportunities within one generation pass, not
    across the product's whole history.
    """
    frequency = min(1.0, statistics.doc_count_share or 0.0)
    negativity = statistics.negativity_rate or 0.0
    sentiment_magnitude = abs(statistics.sentiment_mean) if statistics.sentiment_mean is not None else 0.0
    severity = negativity * sentiment_magnitude
    momentum = statistics.trend_score or 0.0
    breadth = statistics.breadth_ratio
    intent_purity = sum(statistics.intent_mix.get(intent, 0.0) for intent in _ACTIONABLE_INTENTS)
    staleness = 1.0 / (1.0 + days_since_last_mention)

    components = OpportunityScoreComponents(
        frequency=frequency, severity=severity, momentum=momentum, breadth=breadth,
        intent_purity=intent_purity, staleness=staleness,
    )

    value = (
        weights.frequency * components.frequency
        + weights.severity * components.severity
        + weights.momentum * components.momentum
        + weights.breadth * components.breadth
        + weights.intent_purity * components.intent_purity
        + weights.staleness * components.staleness
    )

    return OpportunityScore(value=value, components=dataclasses.asdict(components))
