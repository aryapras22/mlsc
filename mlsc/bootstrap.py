"""Common startup coordinator: settings validation, client creation, read-only checks."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.backoff import ExponentialBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mlsc.config import (
    POSTGRES_SERVICE,
    REDIS_SERVICE,
    PostgresSettings,
    RedisSettings,
    Settings,
    load_settings,
)
from mlsc.db.session import build_engine, build_session_factory

EXPECTED_POSTGRES_MAJOR = 16
REQUIRED_EXTENSIONS: tuple[str, ...] = ("timescaledb", "vector")

READ_ONLY_TRANSACTION = text("SET TRANSACTION READ ONLY")
SERVER_VERSION_NUM = text("SELECT current_setting('server_version_num')")
INSTALLED_EXTENSIONS = text("SELECT extname FROM pg_extension WHERE extname IN :names").bindparams(
    bindparam("names", expanding=True)
)


class StartupError(RuntimeError):
    """Raised when a dependency check fails; the process must not reach ready state."""


@dataclass
class ReadinessGate:
    """Readiness of one process. Never shared across processes."""

    released: bool = False

    def release(self) -> None:
        self.released = True


@dataclass(frozen=True)
class StartupResult:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    readiness: ReadinessGate


def build_redis_client(settings: RedisSettings) -> Redis:
    """Create the client for the configured endpoint only; it holds no alternate target."""
    return Redis(
        host=settings.host,
        port=settings.port,
        db=settings.database,
        username=settings.username,
        password=settings.password.get_secret_value() if settings.password else None,
        ssl=settings.use_tls,
        ssl_ca_certs=settings.ssl_ca_cert,
        socket_timeout=settings.socket_timeout_seconds,
        socket_connect_timeout=settings.socket_connect_timeout_seconds,
        retry=Retry(ExponentialBackoff(), settings.max_retries),
    )


async def check_postgres_capabilities(engine: AsyncEngine, endpoint: str) -> None:
    """Read version and extension catalogs in a read-only transaction, then roll back."""
    try:
        async with engine.connect() as connection:
            await connection.execute(READ_ONLY_TRANSACTION)
            version_value = (await connection.execute(SERVER_VERSION_NUM)).scalar_one_or_none()
            installed = (
                await connection.execute(
                    INSTALLED_EXTENSIONS, {"names": list(REQUIRED_EXTENSIONS)}
                )
            ).scalars().all()
            await connection.rollback()
    except SQLAlchemyError as error:
        raise StartupError(
            f"{POSTGRES_SERVICE} at {endpoint} is unreachable: {type(error).__name__}"
        ) from error

    _require_expected_major(version_value, endpoint)
    _require_installed_extensions(installed, endpoint)


async def check_redis(client: Redis, endpoint: str) -> None:
    try:
        acknowledged = await client.ping()
    except (RedisError, OSError) as error:
        raise StartupError(
            f"{REDIS_SERVICE} at {endpoint} is unreachable: {type(error).__name__}"
        ) from error
    if not acknowledged:
        raise StartupError(f"{REDIS_SERVICE} at {endpoint} did not acknowledge a ping")


async def start_process(
    *,
    settings: Settings | None = None,
    engine_factory: Callable[[PostgresSettings], AsyncEngine] = build_engine,
    redis_factory: Callable[[RedisSettings], Redis] = build_redis_client,
) -> StartupResult:
    """Validate settings, check the configured dependencies, then release this process's gate."""
    resolved = settings if settings is not None else load_settings()

    engine = engine_factory(resolved.postgres)
    redis_client = redis_factory(resolved.redis)
    try:
        await check_postgres_capabilities(engine, resolved.postgres.sanitized_endpoint)
        await check_redis(redis_client, resolved.redis.sanitized_endpoint)
    except BaseException:
        await _release_clients(engine, redis_client)
        raise

    readiness = ReadinessGate()
    readiness.release()
    return StartupResult(
        settings=resolved,
        engine=engine,
        session_factory=build_session_factory(engine),
        redis=redis_client,
        readiness=readiness,
    )


def _require_expected_major(value: object, endpoint: str) -> None:
    try:
        version_num = int(str(value))
    except ValueError:
        raise StartupError(
            f"{POSTGRES_SERVICE} at {endpoint} returned an indeterminate server version; "
            f"expected major version {EXPECTED_POSTGRES_MAJOR}"
        ) from None
    detected = version_num // 10000
    if detected != EXPECTED_POSTGRES_MAJOR:
        raise StartupError(
            f"{POSTGRES_SERVICE} at {endpoint} reports major version {detected}; "
            f"expected major version {EXPECTED_POSTGRES_MAJOR}"
        )


def _require_installed_extensions(rows: Sequence[object], endpoint: str) -> None:
    if any(not isinstance(row, str) or not row for row in rows):
        raise StartupError(
            f"{POSTGRES_SERVICE} at {endpoint} returned an indeterminate extension catalog result "
            f"for {', '.join(REQUIRED_EXTENSIONS)}"
        )
    installed = set(rows)
    for extension in REQUIRED_EXTENSIONS:
        if extension not in installed:
            raise StartupError(
                f"{POSTGRES_SERVICE} at {endpoint} does not have required extension "
                f"'{extension}' installed"
            )


async def _release_clients(engine: AsyncEngine, redis_client: Redis) -> None:
    with contextlib.suppress(Exception):
        await engine.dispose()
    with contextlib.suppress(Exception):
        await redis_client.aclose()
