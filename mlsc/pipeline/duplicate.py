"""Simhash near-duplicate detection: flagged, never dropped (requirement 6).

Implemented directly rather than via a dependency: simhash is a small,
well-known algorithm (64-bit fingerprint from weighted token hashes,
compared by Hamming distance), and pulling in a library for it here would be
one more dependency-approval round for a few lines of code.
"""

from __future__ import annotations

import hashlib
import re

_TOKEN_PATTERN = re.compile(r"\w+")
_HAMMING_THRESHOLD = 3


def simhash(text: str) -> int:
    tokens = _TOKEN_PATTERN.findall(text.lower())
    if not tokens:
        return 0
    bit_weights = [0] * 64
    for token in tokens:
        digest = int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:8], "big")
        for bit in range(64):
            bit_weights[bit] += 1 if (digest >> bit) & 1 else -1
    fingerprint = 0
    for bit in range(64):
        if bit_weights[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_near_duplicate(a: int, b: int) -> bool:
    return hamming_distance(a, b) <= _HAMMING_THRESHOLD
