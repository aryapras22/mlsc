"""The enrichment stage set, kept dependency-free.

Split out of `mlsc/tasks/enrich.py` so naming a stage — in a request schema,
a repair override — does not pull in that module's heavy pipeline imports
(sentence-transformers, the LLM router) just to validate a string against a
closed set (monitor-repair-overrides design.md, "Trust boundary").
"""

from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    CLEAN = "clean"
    LANGUAGE = "language"
    RELEVANCE = "relevance"
    DUPLICATE = "duplicate"
    EMBED = "embed"
    SENTIMENT = "sentiment"
    INTENT = "intent"


ALL_STAGES = frozenset(Stage)
