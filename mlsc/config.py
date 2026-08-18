"""Managed-service settings models validated before any client is constructed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

POSTGRES_SERVICE = "postgresql"
REDIS_SERVICE = "redis"

PostgresSslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]
_VERIFYING_SSL_MODES = ("verify-ca", "verify-full")


class ConfigurationError(RuntimeError):
    """Raised when managed-service settings are missing, malformed, or inconsistent."""


class PostgresSettings(BaseSettings):
    """PostgreSQL endpoint, credentials, transport security, timeouts, and pooling.

    Endpoint fields have no defaults: an unset endpoint must fail validation rather
    than resolve to localhost or any other substitute target (requirement 2.11).
    """

    model_config = SettingsConfigDict(env_prefix="MLSC_POSTGRES_", frozen=True, extra="ignore")

    service_name: ClassVar[str] = POSTGRES_SERVICE

    host: str
    port: int = Field(ge=1, le=65535)
    database: str
    user: str
    password: SecretStr
    ssl_mode: PostgresSslMode = "require"
    ssl_root_cert: str | None = None
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    command_timeout_seconds: float = Field(default=30.0, gt=0)
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=5, ge=0)
    pool_recycle_seconds: int = Field(default=-1, ge=-1)

    @field_validator("host", "database", "user")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def _require_consistent_tls_inputs(self) -> PostgresSettings:
        verifying = self.ssl_mode in _VERIFYING_SSL_MODES
        if verifying and not self.ssl_root_cert:
            raise ValueError(f"ssl_root_cert is required when ssl_mode is {self.ssl_mode}")
        if not verifying and self.ssl_root_cert:
            raise ValueError(f"ssl_root_cert is unusable when ssl_mode is {self.ssl_mode}")
        return self

    @property
    def sanitized_endpoint(self) -> str:
        return f"postgresql://{self.host}:{self.port}/{self.database}"


class RedisSettings(BaseSettings):
    """Redis endpoint, credentials, transport security, timeouts, and retry limits."""

    model_config = SettingsConfigDict(env_prefix="MLSC_REDIS_", frozen=True, extra="ignore")

    service_name: ClassVar[str] = REDIS_SERVICE

    host: str
    port: int = Field(ge=1, le=65535)
    database: int = Field(default=0, ge=0)
    username: str | None = None
    password: SecretStr | None = None
    use_tls: bool = True
    ssl_ca_cert: str | None = None
    socket_timeout_seconds: float = Field(default=10.0, gt=0)
    socket_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=3, ge=0)

    @field_validator("host")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped

    @model_validator(mode="after")
    def _require_consistent_tls_inputs(self) -> RedisSettings:
        if self.ssl_ca_cert and not self.use_tls:
            raise ValueError("ssl_ca_cert is unusable when use_tls is false")
        return self

    @property
    def sanitized_endpoint(self) -> str:
        scheme = "rediss" if self.use_tls else "redis"
        return f"{scheme}://{self.host}:{self.port}/{self.database}"


@dataclass(frozen=True)
class Settings:
    postgres: PostgresSettings
    redis: RedisSettings


def load_settings() -> Settings:
    """Validate every required setting from the process environment, without network access."""
    return Settings(postgres=_load(PostgresSettings), redis=_load(RedisSettings))


def _load[SettingsT: BaseSettings](model: type[SettingsT]) -> SettingsT:
    try:
        return model()
    except ValidationError as error:
        # The cause is dropped deliberately: pydantic renders rejected input values,
        # which would expose credentials in an operator-visible traceback.
        raise ConfigurationError(_render_invalid_settings(model, error)) from None


def _render_invalid_settings(model: type[BaseSettings], error: ValidationError) -> str:
    prefix = model.model_config.get("env_prefix", "")
    problems = []
    for detail in error.errors():
        if detail["loc"]:
            field = ".".join(str(part) for part in detail["loc"])
            source = f"{prefix}{field}".upper()
        else:
            field = "cross-field"
            source = "cross-field validation"
        problems.append(f"{field} ({source}): {detail['msg']}")
    service = getattr(model, "service_name", model.__name__)
    return f"{service} configuration is invalid: " + "; ".join(problems)
