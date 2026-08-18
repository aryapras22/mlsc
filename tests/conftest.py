"""Shared fixtures for foundation unit tests.

Requirements: 2.3-2.5, 2.7-2.11, 3.1-3.10.
"""

from __future__ import annotations

import os

import pytest

VALID_ENVIRONMENT = {
    "MLSC_POSTGRES_HOST": "pg.provider.example",
    "MLSC_POSTGRES_PORT": "6543",
    "MLSC_POSTGRES_DATABASE": "mlsc",
    "MLSC_POSTGRES_USER": "mlsc_app",
    "MLSC_POSTGRES_PASSWORD": "postgres-secret-value",
    "MLSC_REDIS_HOST": "redis.provider.example",
    "MLSC_REDIS_PORT": "6380",
    "MLSC_REDIS_PASSWORD": "redis-secret-value",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every managed-service variable so a developer shell cannot supply one."""
    for name in list(os.environ):
        if name.startswith("MLSC_"):
            monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def valid_env(clean_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in VALID_ENVIRONMENT.items():
        clean_env.setenv(name, value)
    return clean_env
