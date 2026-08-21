"""Novelty: a topic drawing vocabulary nobody used before, even without a
volume anomaly.

Compares today's documents' c-TF-IDF-distinctive terms against the terms
that were already distinctive for this topic over its own recent history.
A topic can be otherwise unremarkable in volume and sentiment while still
being worth a user's attention because people are suddenly describing it
differently — "Byfron" appearing for the first time inside an otherwise
steady "anti-cheat complaints" topic, for instance.
"""

from __future__ import annotations

from sklearn.feature_extraction.text import TfidfVectorizer

from mlsc.db.models import DetectionMethod, Direction
from mlsc.pipeline.analytics.detectors.base import TestResult

_TOP_KEYWORDS = 10
_NOVELTY_RATIO_THRESHOLD = 0.5


def test(today_texts: list[str], history_texts: list[str]) -> TestResult | None:
    """``today_texts`` and ``history_texts`` are each a topic's document
    bodies — today's versus its own trailing window's — kept as two pseudo-
    documents the same way discovery treats a candidate cluster."""
    if not today_texts or not history_texts:
        return None

    vectorizer = TfidfVectorizer(max_features=2000, stop_words="english")
    pseudo_documents = [" ".join(today_texts), " ".join(history_texts)]
    matrix = vectorizer.fit_transform(pseudo_documents)
    terms = vectorizer.get_feature_names_out()

    today_row = matrix[0].toarray()[0]
    history_row = matrix[1].toarray()[0]

    today_top = _top_terms(today_row, terms)
    history_top = _top_terms(history_row, terms)
    if not today_top:
        return None

    new_terms = today_top - history_top
    novelty_ratio = len(new_terms) / len(today_top)

    if novelty_ratio < _NOVELTY_RATIO_THRESHOLD:
        return None

    return TestResult(
        method=DetectionMethod.CTFIDF_DELTA,
        statistic=novelty_ratio,
        p_value=0.0,  # a vocabulary-overlap measure, not a hypothesis test
        observed=novelty_ratio,
        expected=0.0,
        direction=Direction.RISING,
    )


def _top_terms(row, terms) -> set[str]:
    top_indices = row.argsort()[::-1][:_TOP_KEYWORDS]
    return {terms[i] for i in top_indices if row[i] > 0}
