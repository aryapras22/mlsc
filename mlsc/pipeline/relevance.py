"""Relevance scoring.

The threshold and its basis are an open decision in the spec (calibration
against a hand-checked sample, task 15). This is a minimal length-based floor
so the pipeline stage exists and is swappable; it is not the calibrated
version.
"""

from __future__ import annotations

import dataclasses

from sklearn.metrics.pairwise import cosine_similarity

_MIN_RELEVANT_LENGTH = 3


def score_relevance(text: str | None) -> float:
    if not text:
        return 0.0
    word_count = len(text.split())
    if word_count < _MIN_RELEVANT_LENGTH:
        return 0.0
    return 1.0


def is_relevant(score: float) -> bool:
    return score > 0.0


@dataclasses.dataclass(frozen=True)
class RelevanceVerdict:
    score: float
    is_relevant: bool


class ThemeRelevanceScorer:
    """Scores a theme monitor's document by embedding similarity against a
    caller-supplied reference set (theme-monitors requirement 7).

    A theme has no fixed target the way a product monitor does (learn.md,
    "A theme's boundary is fuzzy"), so this replaces the length floor for
    theme monitors only. Which basis the reference embeddings come from —
    the description, the accepted queries, or the corpus centroid — is
    decided by the caller, not this scorer: the scorer only ever compares
    two sets of vectors, so a later change to the basis is a call-site
    substitution, not a rewrite here (design.md, "Dependencies, injected").
    """

    def __init__(self, *, threshold: float) -> None:
        self._threshold = threshold

    def score(
        self, document_embedding: list[float], reference_embeddings: list[list[float]]
    ) -> RelevanceVerdict:
        if not reference_embeddings:
            return RelevanceVerdict(score=0.0, is_relevant=False)
        similarities = cosine_similarity([document_embedding], reference_embeddings)[0]
        score = float(max(similarities))
        return RelevanceVerdict(score=score, is_relevant=score >= self._threshold)
