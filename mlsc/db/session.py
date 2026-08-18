"""Process-local async engine and session factory; never creates schema."""

from __future__ import annotations

import ssl

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mlsc.config import PostgresSettings

_VERIFYING_SSL_MODES = ("verify-ca", "verify-full")


def build_engine(settings: PostgresSettings) -> AsyncEngine:
    """Create the engine for the configured endpoint only; it holds no alternate target."""
    return create_async_engine(
        build_url(settings),
        connect_args={
            "timeout": settings.connect_timeout_seconds,
            "command_timeout": settings.command_timeout_seconds,
            "ssl": _ssl_argument(settings),
        },
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_recycle=settings.pool_recycle_seconds,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def build_url(settings: PostgresSettings) -> URL:
    return URL.create(
        "postgresql+asyncpg",
        username=settings.user,
        password=settings.password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        database=settings.database,
    )


def _ssl_argument(settings: PostgresSettings) -> ssl.SSLContext | str:
    if settings.ssl_mode not in _VERIFYING_SSL_MODES:
        return settings.ssl_mode
    context = ssl.create_default_context(cafile=settings.ssl_root_cert)
    context.check_hostname = settings.ssl_mode == "verify-full"
    context.verify_mode = ssl.CERT_REQUIRED
    return context
