"""Author hashing and content hashing, applied before any row is written.

PII stripping is not optional (C11): no raw username reaches the database.
"""

from __future__ import annotations

import hashlib


def hash_author(username: str) -> str:
    """One-way hash of an author identifier. Never reversible, never salted per-row.

    Unsalted so the same author hashes identically across documents and runs,
    which is what makes author-diversity metrics possible later without
    storing the username itself.
    """
    return hashlib.sha256(username.encode("utf-8")).hexdigest()


def hash_content(*parts: str | None) -> str:
    """Hash of a document's meaningful content, used to detect exact duplicates."""
    joined = "\x1f".join(part or "" for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


import re

_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_PATTERN = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
_URL_PATTERN = re.compile(r"https?://\S+")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def clean_text(raw: str | None) -> str | None:
    """Normalise whitespace and strip nothing meaningful. Returns None if
    nothing survives cleaning (TextUnusable's fall-back case)."""
    if raw is None:
        return None
    cleaned = _WHITESPACE_PATTERN.sub(" ", raw).strip()
    return cleaned or None


def strip_pii(text: str | None) -> str | None:
    """Removes emails, phone numbers, and URLs from body text.

    Author identity is handled separately by hash_author; this only covers
    PII that can appear inside the document body itself (requirement 4).
    """
    if text is None:
        return None
    stripped = _EMAIL_PATTERN.sub("[email]", text)
    stripped = _PHONE_PATTERN.sub("[phone]", stripped)
    stripped = _URL_PATTERN.sub("[link]", stripped)
    return stripped
