"""The composite ranking score: which topic moved most today, combining
burst strength, growth, novelty, corroborating sources and sentiment
movement into one number a user can sort by.

Normalised across the day's own topics rather than against an absolute
range (design.md, "Alternatives": "a ranking only needs to be correct
within its day"), then reduced by a concentration penalty when a single
author dominates, and withheld outright on an untrustworthy day rather than
computed from a floor — a score built from bad data would look exactly as
confident as a real one (requirements.md, C5/C6).
"""

from __future__ import annotations

import dataclasses
import uuid

from mlsc.config import TrendDetectionSettings
from mlsc.db.models import DailyMetric, EventKind, TrendEvent

_CONCENTRATION_THRESHOLD = 0.5
_CONCENTRATION_PENALTY = 0.5


@dataclasses.dataclass(frozen=True)
class ScoreComponents:
    burst: float
    growth: float
    novelty: float
    breadth_ratio: float
    sentiment_delta: float
    volume: float


@dataclasses.dataclass(frozen=True)
class TopicScoreInput:
    topic_id: uuid.UUID
    events: list[TrendEvent]
    metric: DailyMetric | None
    author_diversity: float | None
    active_source_count: int
    sources_with_topic: int


@dataclasses.dataclass(frozen=True)
class TopicScore:
    topic_id: uuid.UUID
    value: float | None
    components: dict | None
    penalties: dict
    withheld_reason: str | None


def breadth_ratio(sources_with_topic: int, active_source_count: int) -> float:
    """Requirement 10: corroboration is a proportion of the day's active
    sources, never a raw count — two mentions out of two active sources is
    stronger evidence than two mentions out of ten."""
    if active_source_count <= 0:
        return 0.0
    return sources_with_topic / active_source_count


def score_topics(
    inputs: list[TopicScoreInput], *, settings: TrendDetectionSettings, day_is_trustworthy: bool
) -> list[TopicScore]:
    """One score per topic with metrics that day, normalised across this
    same list — the question a ranking exists to answer is "who moved most
    today", not "how does today compare to every day this monitor has run".
    """
    if not day_is_trustworthy:
        return [
            TopicScore(topic_id=item.topic_id, value=None, components=None, penalties={},
                       withheld_reason="day_untrustworthy")
            for item in inputs
        ]

    raw_components = [_raw_components(item) for item in inputs]
    normalised = _normalise_components(raw_components)

    scores: list[TopicScore] = []
    for item, components in zip(inputs, normalised, strict=True):
        if item.metric is None:
            scores.append(
                TopicScore(topic_id=item.topic_id, value=None, components=None, penalties={},
                           withheld_reason="no_metric_for_day")
            )
            continue

        weighted = (
            settings.weight_burst * components.burst
            + settings.weight_growth * components.growth
            + settings.weight_novelty * components.novelty
            + settings.weight_breadth * components.breadth_ratio
            + settings.weight_sentiment * components.sentiment_delta
        )

        penalties: dict[str, float] = {}
        value = weighted
        if item.author_diversity is not None and item.author_diversity < _CONCENTRATION_THRESHOLD:
            penalties["concentration"] = _CONCENTRATION_PENALTY
            value *= 1 - _CONCENTRATION_PENALTY

        scores.append(
            TopicScore(
                topic_id=item.topic_id, value=value,
                components=dataclasses.asdict(components), penalties=penalties, withheld_reason=None,
            )
        )

    return scores


def _raw_components(item: TopicScoreInput) -> ScoreComponents:
    burst_events = [event for event in item.events if event.kind == EventKind.BURST]
    growth_events = [
        event for event in item.events if event.kind in (EventKind.SUSTAINED_GROWTH, EventKind.DECLINE)
    ]
    novelty_events = [
        event for event in item.events if event.kind in (EventKind.NOVELTY, EventKind.EMERGENCE)
    ]
    sentiment_events = [event for event in item.events if event.kind == EventKind.SENTIMENT_FLIP]

    burst = max((event.severity for event in burst_events), default=0.0)
    growth = max((event.severity for event in growth_events), default=0.0)
    novelty = max((event.severity for event in novelty_events), default=0.0)
    sentiment_delta = max((event.severity for event in sentiment_events), default=0.0)
    breadth = breadth_ratio(item.sources_with_topic, item.active_source_count)
    volume = float(item.metric.doc_count) if item.metric is not None else 0.0

    return ScoreComponents(
        burst=burst, growth=growth, novelty=novelty, breadth_ratio=breadth,
        sentiment_delta=sentiment_delta, volume=volume,
    )


def _normalise_components(raw: list[ScoreComponents]) -> list[ScoreComponents]:
    """Each component divided by the day's own maximum, so every component
    lands in ``[0, 1]`` before weighting — comparable within the day, which
    is the only comparison a ranking needs (design.md, "Success path")."""
    if not raw:
        return []

    max_burst = max((c.burst for c in raw), default=0.0) or 1.0
    max_growth = max((c.growth for c in raw), default=0.0) or 1.0
    max_novelty = max((c.novelty for c in raw), default=0.0) or 1.0
    max_sentiment = max((c.sentiment_delta for c in raw), default=0.0) or 1.0
    # breadth_ratio is already in [0, 1] by construction; no normalisation needed.

    return [
        ScoreComponents(
            burst=c.burst / max_burst,
            growth=c.growth / max_growth,
            novelty=c.novelty / max_novelty,
            breadth_ratio=c.breadth_ratio,
            sentiment_delta=c.sentiment_delta / max_sentiment,
            volume=c.volume,
        )
        for c in raw
    ]
