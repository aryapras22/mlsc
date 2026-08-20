"""Embedder and sentiment scorer: loaded once per worker process, injected into
the task. Constructing them per task would reload the models (design.md,
"Dependencies, injected")."""

from __future__ import annotations

import dataclasses

from sentence_transformers import SentenceTransformer
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from mlsc.db.models import SentimentLabel

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
SENTIMENT_MODEL_NAME = "vader"


@dataclasses.dataclass(frozen=True)
class SentimentVerdict:
    score: float
    label: SentimentLabel


class Embedder:
    """Wraps sentence-transformers so its version is reportable and it can be
    swapped without touching collection logic."""

    def __init__(self, model: SentenceTransformer | None = None) -> None:
        self._model = model or SentenceTransformer(EMBEDDING_MODEL_NAME)

    def encode(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts).tolist()


class SentimentScorer:
    def __init__(self, analyzer: SentimentIntensityAnalyzer | None = None) -> None:
        self._analyzer = analyzer or SentimentIntensityAnalyzer()

    def score(self, text: str) -> SentimentVerdict:
        compound = self._analyzer.polarity_scores(text)["compound"]
        if compound >= 0.05:
            label = SentimentLabel.POSITIVE
        elif compound <= -0.05:
            label = SentimentLabel.NEGATIVE
        else:
            label = SentimentLabel.NEUTRAL
        return SentimentVerdict(score=compound, label=label)
