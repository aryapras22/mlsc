"""Unit tests for the process-local engine and session factory.

Requirements: 1.3, 2.3, 2.4, 2.7, 2.11.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from mlsc.config import load_settings
from mlsc.db.session import build_engine, build_session_factory, build_url
from tests.conftest import VALID_ENVIRONMENT


def test_url_targets_only_the_configured_endpoint(valid_env: pytest.MonkeyPatch) -> None:
    url = build_url(load_settings().postgres)

    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "pg.provider.example"
    assert url.port == 6543
    assert url.database == "mlsc"
    assert url.username == "mlsc_app"


def test_rendered_url_hides_the_password(valid_env: pytest.MonkeyPatch) -> None:
    url = build_url(load_settings().postgres)

    assert VALID_ENVIRONMENT["MLSC_POSTGRES_PASSWORD"] not in url.render_as_string()
    assert VALID_ENVIRONMENT["MLSC_POSTGRES_PASSWORD"] not in repr(url)


def test_engine_and_session_factory_use_configured_pool_settings(
    valid_env: pytest.MonkeyPatch,
) -> None:
    valid_env.setenv("MLSC_POSTGRES_POOL_SIZE", "3")
    valid_env.setenv("MLSC_POSTGRES_MAX_OVERFLOW", "7")

    engine = build_engine(load_settings().postgres)
    try:
        assert engine.url.host == "pg.provider.example"
        assert engine.sync_engine.pool.size() == 3
        assert build_session_factory(engine).class_ is AsyncSession
    finally:
        engine.sync_engine.dispose()
