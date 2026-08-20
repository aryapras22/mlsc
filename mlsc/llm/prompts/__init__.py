"""Loads a versioned prompt: the version lives in the filename, not a column
the caller has to remember to bump (requirement 3)."""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str, version: str) -> str:
    path = _PROMPTS_DIR / f"{name}_{version}.txt"
    return path.read_text()
