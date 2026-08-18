"""Unit tests for managed-service settings validation.

Requirements: 2.3, 2.4, 2.7, 2.8, 2.9, 2.11.
"""

from __future__ import annotations

import pytest

from mlsc.config import ConfigurationError, load_settings
from tests.conftest import VALID_ENVIRONMENT

SECRETS = ("postgres-secret-value", "redis-secret-value")


def test_configured_endpoints_are_used_verbatim(valid_env: pytest.MonkeyPatch) -> None:
    settings = load_settings()

    assert settings.postgres.host == "pg.provider.example"
    assert settings.postgres.port == 6543
    assert settings.postgres.database == "mlsc"
    assert settings.redis.host == "redis.provider.example"
    assert settings.redis.port == 6380


def test_sanitized_endpoints_exclude_credentials(valid_env: pytest.MonkeyPatch) -> None:
    settings = load_settings()

    assert settings.postgres.sanitized_endpoint == "postgresql://pg.provider.example:6543/mlsc"
    assert settings.redis.sanitized_endpoint == "rediss://redis.provider.example:6380/0"
    assert "mlsc_app" not in settings.postgres.sanitized_endpoint
    for secret in SECRETS:
        assert secret not in settings.postgres.sanitized_endpoint
        assert secret not in settings.redis.sanitized_endpoint


@pytest.mark.parametrize(
    ("variable", "service", "field"),
    [
        ("MLSC_POSTGRES_HOST", "postgresql", "host"),
        ("MLSC_POSTGRES_PORT", "postgresql", "port"),
        ("MLSC_POSTGRES_DATABASE", "postgresql", "database"),
        ("MLSC_POSTGRES_USER", "postgresql", "user"),
        ("MLSC_POSTGRES_PASSWORD", "postgresql", "password"),
        ("MLSC_REDIS_HOST", "redis", "host"),
        ("MLSC_REDIS_PORT", "redis", "port"),
    ],
)
def test_missing_required_setting_names_service_and_field(
    valid_env: pytest.MonkeyPatch, variable: str, service: str, field: str
) -> None:
    valid_env.delenv(variable)

    with pytest.raises(ConfigurationError) as failure:
        load_settings()

    message = str(failure.value)
    assert service in message
    assert field in message
    assert variable in message


def test_missing_endpoint_never_falls_back_to_localhost(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError) as failure:
        load_settings()

    message = str(failure.value)
    assert "localhost" not in message
    assert "127.0.0.1" not in message
    assert "5432" not in message


@pytest.mark.parametrize("value", ["not-a-port", "0", "70000", ""])
def test_malformed_postgres_port_is_rejected(
    valid_env: pytest.MonkeyPatch, value: str
) -> None:
    valid_env.setenv("MLSC_POSTGRES_PORT", value)

    with pytest.raises(ConfigurationError) as failure:
        load_settings()

    assert "port" in str(failure.value)


@pytest.mark.parametrize("value", ["", "   ", "\u00a0"])
def test_blank_postgres_host_is_rejected(valid_env: pytest.MonkeyPatch, value: str) -> None:
    valid_env.setenv("MLSC_POSTGRES_HOST", value)

    with pytest.raises(ConfigurationError) as failure:
        load_settings()

    assert "host" in str(failure.value)


def test_verifying_tls_mode_requires_a_root_certificate(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv("MLSC_POSTGRES_SSL_MODE", "verify-full")

    with pytest.raises(ConfigurationError) as failure:
        load_settings()

    message = str(failure.value)
    assert "postgresql" in message
    assert "ssl_root_cert" in message


def test_root_certificate_without_a_verifying_tls_mode_is_inconsistent(
    valid_env: pytest.MonkeyPatch,
) -> None:
    valid_env.setenv("MLSC_POSTGRES_SSL_MODE", "require")
    valid_env.setenv("MLSC_POSTGRES_SSL_ROOT_CERT", "/etc/ssl/provider-ca.pem")

    with pytest.raises(ConfigurationError) as failure:
        load_settings()

    assert "ssl_root_cert" in str(failure.value)


def test_redis_ca_certificate_without_tls_is_inconsistent(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv("MLSC_REDIS_USE_TLS", "false")
    valid_env.setenv("MLSC_REDIS_SSL_CA_CERT", "/etc/ssl/provider-ca.pem")

    with pytest.raises(ConfigurationError) as failure:
        load_settings()

    message = str(failure.value)
    assert "redis" in message
    assert "ssl_ca_cert" in message


def test_validation_errors_never_render_secrets(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv("MLSC_POSTGRES_PORT", "not-a-port")

    with pytest.raises(ConfigurationError) as failure:
        load_settings()

    rendered = repr(failure.value)
    assert failure.value.__cause__ is None
    for secret in SECRETS:
        assert secret not in rendered


def test_secret_values_stay_wrapped(valid_env: pytest.MonkeyPatch) -> None:
    settings = load_settings()

    assert settings.postgres.password.get_secret_value() == VALID_ENVIRONMENT[
        "MLSC_POSTGRES_PASSWORD"
    ]
    for rendered in (str(settings.postgres), repr(settings.postgres), repr(settings.redis)):
        for secret in SECRETS:
            assert secret not in rendered


def test_operational_settings_have_validated_bounds(valid_env: pytest.MonkeyPatch) -> None:
    valid_env.setenv("MLSC_POSTGRES_POOL_SIZE", "0")
    with pytest.raises(ConfigurationError) as failure:
        load_settings()
    assert "pool_size" in str(failure.value)

    valid_env.setenv("MLSC_POSTGRES_POOL_SIZE", "4")
    valid_env.setenv("MLSC_POSTGRES_CONNECT_TIMEOUT_SECONDS", "0")
    with pytest.raises(ConfigurationError) as failure:
        load_settings()
    assert "connect_timeout_seconds" in str(failure.value)

    valid_env.setenv("MLSC_POSTGRES_CONNECT_TIMEOUT_SECONDS", "7.5")
    valid_env.setenv("MLSC_REDIS_MAX_RETRIES", "-1")
    with pytest.raises(ConfigurationError) as failure:
        load_settings()
    assert "max_retries" in str(failure.value)
