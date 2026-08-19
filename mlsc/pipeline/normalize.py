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
