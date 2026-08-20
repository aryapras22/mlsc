"""Relevance scoring.

The threshold and its basis are an open decision in the spec (calibration
against a hand-checked sample, task 15). This is a minimal length-based floor
so the pipeline stage exists and is swappable; it is not the calibrated
version.
"""

from __future__ import annotations

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
