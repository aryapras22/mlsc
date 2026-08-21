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


class LlmTierSettings(BaseSettings):
    """One tier's provider endpoint and model, selected by env vars alone (C2).

    Env prefix carries the tier name: MLSC_LLM_INTENT_*, MLSC_LLM_LABELING_*,
    MLSC_LLM_INSIGHT_*. Any OpenAI-compatible endpoint works — Ollama, vLLM,
    or a cloud provider — because only the base_url and api_key differ.
    """

    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    base_url: str
    api_key: SecretStr = SecretStr("not-required")
    model: str


class TopicThresholds(BaseSettings):
    """Every similarity and agreement threshold `persistent-topics` needs.

    Every value here is explicitly unresolved in the spec and will be
    re-tuned against real data (design.md, "Dependencies, injected") — this
    is one injected configuration value rather than scattered constants, so
    retuning is an environment change, not a code change.
    """

    model_config = SettingsConfigDict(env_prefix="MLSC_TOPICS_", frozen=True, extra="ignore")

    assignment_threshold: float = Field(default=0.55, gt=0, lt=1)
    merge_threshold: float = Field(default=0.75, gt=0, lt=1)
    drift_factor: float = Field(default=0.02, gt=0, lt=1)
    min_residue_pool_size: int = Field(default=30, ge=1)
    min_cluster_size: int = Field(default=5, ge=2)
    dormancy_days: int = Field(default=60, ge=1)
    refit_agreement_threshold: float = Field(default=0.6, gt=0, le=1)
    refit_window_days: int = Field(default=30, ge=1)
    split_drift_threshold: float = Field(default=0.5, gt=0)

    @model_validator(mode="after")
    def _require_merge_above_assignment(self) -> TopicThresholds:
        # A merge threshold at or below the assignment threshold would let
        # discovery immediately re-absorb what assignment just decided was
        # too dissimilar to join, collapsing the registry into one topic.
        if self.merge_threshold <= self.assignment_threshold:
            raise ValueError("merge_threshold must be greater than assignment_threshold")
        return self


class TrendDetectionSettings(BaseSettings):
    """Every floor, cooldown, weight and correction level `trend-detection`
    needs. Every value here is explicitly unresolved in the spec and will be
    re-tuned against the 60-day backfill (requirements.md, "Open decisions")
    — validated at startup so a zero cooldown or a below-minimum volume
    floor cannot silently disable a gate C9 makes mandatory.
    """

    model_config = SettingsConfigDict(env_prefix="MLSC_TREND_", frozen=True, extra="ignore")

    min_volume_floor: int = Field(default=5, ge=1)
    min_clean_baseline_days: int = Field(default=14, ge=7)
    cooldown_days: int = Field(default=3, ge=1)
    fdr_alpha: float = Field(default=0.1, gt=0, lt=1)
    burst_z_threshold: float = Field(default=3.5, gt=0)
    weight_burst: float = Field(default=0.3, ge=0)
    weight_growth: float = Field(default=0.2, ge=0)
    weight_novelty: float = Field(default=0.15, ge=0)
    weight_breadth: float = Field(default=0.2, ge=0)
    weight_sentiment: float = Field(default=0.15, ge=0)

    @model_validator(mode="after")
    def _require_weights_sum_positive(self) -> TrendDetectionSettings:
        # A score built from weights that sum to zero would rank every topic
        # identically, which is indistinguishable from the ranking never
        # having run at all.
        total = (
            self.weight_burst + self.weight_growth + self.weight_novelty
            + self.weight_breadth + self.weight_sentiment
        )
        if total <= 0:
            raise ValueError("score weights must sum to a positive value")
        return self


@dataclass(frozen=True)
class Settings:
    postgres: PostgresSettings
    redis: RedisSettings


def load_settings() -> Settings:
    """Validate every required setting from the process environment, without network access."""
    return Settings(postgres=_load(PostgresSettings), redis=_load(RedisSettings))


def load_topic_thresholds() -> TopicThresholds:
    """Validated at startup so a merge threshold below the assignment threshold
    cannot start (requirement 1, 3, 7)."""
    return _load(TopicThresholds)


def load_trend_detection_settings() -> TrendDetectionSettings:
    """Validated at startup so no gate or score weight can be silently
    disabled by a zero (requirement 4, 6, 7, 9)."""
    return _load(TrendDetectionSettings)


def load_llm_tier_settings(tier: str) -> LlmTierSettings:
    """Raises ConfigurationError if the tier has no configured endpoint.

    Called at startup, not at first use — a misconfigured tier must fail
    before any batch runs, not three hours into one (design.md, "Failure
    strategy": TierNotConfigured crashes at startup).
    """
    prefix = f"MLSC_LLM_{tier.upper()}_"

    class _TierSettings(LlmTierSettings):
        model_config = SettingsConfigDict(env_prefix=prefix, frozen=True, extra="ignore")

    try:
        return _TierSettings()
    except ValidationError as error:
        raise ConfigurationError(_render_invalid_settings(_TierSettings, error)) from None


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
