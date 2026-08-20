"""Language detection. A filtered outcome, never a delete (requirement 5)."""

from __future__ import annotations

import dataclasses

from langdetect import LangDetectException, detect_langs


@dataclasses.dataclass(frozen=True)
class LanguageVerdict:
    code: str
    confidence: float


def detect_language(text: str | None) -> LanguageVerdict | None:
    if not text:
        return None
    try:
        candidates = detect_langs(text)
    except LangDetectException:
        return None
    if not candidates:
        return None
    top = candidates[0]
    return LanguageVerdict(code=top.lang, confidence=top.prob)
