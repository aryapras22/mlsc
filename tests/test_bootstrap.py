"""Unit tests for the read-only startup coordinator.

Requirements: 2.8-2.11, 3.1-3.10.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from mlsc.bootstrap import (
    StartupError,
    check_postgres_capabilities,
    check_redis,
    start_process,
)
from mlsc.config import ConfigurationError, PostgresSettings, RedisSettings, Settings

ENDPOINT = "postgresql://pg.provider.example:6543/mlsc"
REDIS_ENDPOINT = "rediss://redis.provider.example:6380/0"
READ_ONLY_STATEMENT = "SET TRANSACTION READ ONLY"


class FakeResult:
    def __init__(self, value: object = None, rows: tuple[object, ...] = ()) -> None:
        self._value = value
        self._rows = rows

    def scalar_one_or_none(self) -> object:
        return self._value

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list[object]:
        return list(self._rows)


class FakeConnection:
    def __init__(
        self,
        version: object = "160014",
        extensions: tuple[object, ...] = ("timescaledb", "vector"),
        read_error: Exception | None = None,
    ) -> None:
        self.version = version
        self.extensions = extensions
        self.read_error = read_error
        self.statements: list[str] = []
        self.rolled_back = False
        self.committed = False

    async def execute(self, statement: Any, params: Any = None) -> FakeResult:
        sql = str(statement)
        self.statements.append(sql)
        if self.read_error is not None and sql.startswith("SELECT"):
            raise self.read_error
        if "server_version_num" in sql:
            return FakeResult(value=self.version)
        if "pg_extension" in sql:
            return FakeResult(rows=self.extensions)
        return FakeResult()

    async def rollback(self) -> None:
        self.rolled_back = True

    async def commit(self) -> None:
        self.committed = True


class FakeEngine:
    def __init__(
        self, connection: FakeConnection | None = None, connect_error: Exception | None = None
    ) -> None:
        self.connection = connection or FakeConnection()
        self.connect_error = connect_error
        self.disposed = False

    def connect(self) -> _FakeConnectionContext:
        return _FakeConnectionContext(self)

    async def dispose(self) -> None:
        self.disposed = True


class _FakeConnectionContext:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    async def __aenter__(self) -> FakeConnection:
        if self._engine.connect_error is not None:
            raise self._engine.connect_error
        return self._engine.connection

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeRedis:
    def __init__(self, response: object = True, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.pings = 0
        self.closed = False

    async def ping(self) -> object:
        self.pings += 1
        if self.error is not None:
            raise self.error
        return self.response

    async def aclose(self) -> None:
        self.closed = True


def unreachable_postgres() -> OperationalError:
    return OperationalError("SELECT 1", {}, OSError("connection refused"))


def settings_for(postgres_overrides: dict[str, Any] | None = None) -> Settings:
    postgres = PostgresSettings(
        host="pg.provider.example",
        port=6543,
        database="mlsc",
        user="mlsc_app",
        password="postgres-secret-value",
        ssl_mode="require",
        **(postgres_overrides or {}),
    )
    redis = RedisSettings(
        host="redis.provider.example",
        port=6380,
        password="redis-secret-value",
    )
    return Settings(postgres=postgres, redis=redis)


def test_capability_check_reads_only_and_rolls_back() -> None:
    engine = FakeEngine()

    asyncio.run(check_postgres_capabilities(engine, ENDPOINT))

    connection = engine.connection
    assert connection.statements[0] == READ_ONLY_STATEMENT
    assert all(
        statement == READ_ONLY_STATEMENT or statement.startswith("SELECT")
        for statement in connection.statements
    )
    assert connection.rolled_back
    assert not connection.committed


def test_version_mismatch_reports_expected_and_detected_major() -> None:
    engine = FakeEngine(FakeConnection(version="150012"))

    with pytest.raises(StartupError) as failure:
        asyncio.run(check_postgres_capabilities(engine, ENDPOINT))

    message = str(failure.value)
    assert "16" in message
    assert "15" in message
    assert ENDPOINT in message


@pytest.mark.parametrize("version", [None, "", "unknown"])
def test_indeterminate_version_fails_closed(version: object) -> None:
    engine = FakeEngine(FakeConnection(version=version))

    with pytest.raises(StartupError) as failure:
        asyncio.run(check_postgres_capabilities(engine, ENDPOINT))

    message = str(failure.value)
    assert "indeterminate" in message
    assert ENDPOINT in message


@pytest.mark.parametrize(
    ("installed", "missing"),
    [
        (("vector",), "timescaledb"),
        (("timescaledb",), "vector"),
        ((), "timescaledb"),
    ],
)
def test_missing_extension_is_named_with_the_service(
    installed: tuple[str, ...], missing: str
) -> None:
    engine = FakeEngine(FakeConnection(extensions=installed))

    with pytest.raises(StartupError) as failure:
        asyncio.run(check_postgres_capabilities(engine, ENDPOINT))

    message = str(failure.value)
    assert missing in message
    assert "postgresql" in message
    assert ENDPOINT in message


def test_indeterminate_extension_result_fails_closed() -> None:
    engine = FakeEngine(FakeConnection(extensions=(None,)))

    with pytest.raises(StartupError) as failure:
        asyncio.run(check_postgres_capabilities(engine, ENDPOINT))

    assert "indeterminate" in str(failure.value)


def test_connection_failure_names_service_and_sanitized_endpoint() -> None:
    engine = FakeEngine(connect_error=unreachable_postgres())

    with pytest.raises(StartupError) as failure:
        asyncio.run(check_postgres_capabilities(engine, ENDPOINT))

    message = str(failure.value)
    assert "postgresql" in message
    assert ENDPOINT in message
    assert "postgres-secret-value" not in message


def test_redis_failure_names_service_and_sanitized_endpoint() -> None:
    client = FakeRedis(error=RedisConnectionError("refused"))

    with pytest.raises(StartupError) as failure:
        asyncio.run(check_redis(client, REDIS_ENDPOINT))

    message = str(failure.value)
    assert "redis" in message
    assert REDIS_ENDPOINT in message


def test_unacknowledged_redis_ping_fails_closed() -> None:
    client = FakeRedis(response=False)

    with pytest.raises(StartupError) as failure:
        asyncio.run(check_redis(client, REDIS_ENDPOINT))

    assert REDIS_ENDPOINT in str(failure.value)


def test_readiness_is_released_only_after_every_check_passes() -> None:
    engine = FakeEngine()
    client = FakeRedis()

    result = asyncio.run(
        start_process(
            settings=settings_for(),
            engine_factory=lambda _: engine,
            redis_factory=lambda _: client,
        )
    )

    assert result.readiness.released
    assert result.engine is engine
    assert client.pings == 1
    assert not engine.disposed


def test_failed_capability_check_leaves_readiness_unreleased() -> None:
    engine = FakeEngine(FakeConnection(extensions=("timescaledb",)))
    client = FakeRedis()

    with pytest.raises(StartupError):
        asyncio.run(
            start_process(
                settings=settings_for(),
                engine_factory=lambda _: engine,
                redis_factory=lambda _: client,
            )
        )

    assert engine.disposed
    assert client.closed
    assert client.pings == 0


def test_invalid_configuration_constructs_no_client(clean_env: pytest.MonkeyPatch) -> None:
    constructed: list[str] = []

    def record_engine(_: PostgresSettings) -> FakeEngine:
        constructed.append("postgresql")
        return FakeEngine()

    def record_redis(_: RedisSettings) -> FakeRedis:
        constructed.append("redis")
        return FakeRedis()

    with pytest.raises(ConfigurationError) as failure:
        asyncio.run(start_process(engine_factory=record_engine, redis_factory=record_redis))

    assert constructed == []
    assert "localhost" not in str(failure.value)
