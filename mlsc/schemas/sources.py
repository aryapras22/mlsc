"""Request and response contracts for attaching a source to a monitor.

Source configuration is untrusted because it was typed by a user, and is
validated once here, at attach time — not re-checked on every collection
(design.md, "Trust boundary").
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from mlsc.db.models import SourceName

_PLAY_PACKAGE_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")


def _validate_play_config(config: dict[str, Any]) -> str:
    """Check the configured package id and return it as the source's instance key."""
    package_id = config.get("package_id")
    if not isinstance(package_id, str) or not _PLAY_PACKAGE_ID_PATTERN.match(package_id):
        raise ValueError(
            f"config.package_id {package_id!r} is not a well-formed Play package identifier"
        )
    return package_id


def _validate_appstore_config(config: dict[str, Any]) -> str:
    app_id = config.get("app_id")
    if not isinstance(app_id, str) or not app_id.isdigit():
        raise ValueError(f"config.app_id {app_id!r} is not a well-formed App Store numeric id")
    return app_id


def _validated_queries(config: dict[str, Any]) -> list[str]:
    """Check the configured queries, shared by the three kinds that fan out one
    adapter per query."""
    queries = config.get("queries")
    if (
        not isinstance(queries, list)
        or not queries
        or not all(isinstance(query, str) and query.strip() for query in queries)
    ):
        raise ValueError("config.queries must be a non-empty list of non-empty strings")
    return queries


def _validate_discourse_config(config: dict[str, Any]) -> str:
    base_url = config.get("base_url")
    if not isinstance(base_url, str) or not re.match(r"^https?://[^/]+/?$", base_url):
        raise ValueError(f"config.base_url {base_url!r} is not a well-formed forum base URL")
    queries = _validated_queries(config)
    # Two different searches of one forum are two legitimate sources, so the
    # queries belong in the key monitor_sources is unique on, not just the host.
    return "|".join([base_url.rstrip("/"), *queries])


def _validate_news_config(config: dict[str, Any]) -> str:
    return "|".join(_validated_queries(config))


def _validate_feed_config(config: dict[str, Any]) -> str:
    feed_url = config.get("feed_url")
    if not isinstance(feed_url, str) or not re.match(r"^https?://", feed_url):
        raise ValueError(f"config.feed_url {feed_url!r} is not a well-formed URL")
    return feed_url


def _validate_hackernews_config(config: dict[str, Any]) -> str:
    return "|".join(_validated_queries(config))


_INSTANCE_KEY_VALIDATORS = {
    SourceName.PLAY: _validate_play_config,
    SourceName.APPSTORE: _validate_appstore_config,
    SourceName.DISCOURSE: _validate_discourse_config,
    SourceName.NEWS: _validate_news_config,
    SourceName.RSS: _validate_feed_config,
    SourceName.HACKERNEWS: _validate_hackernews_config,
}


def instance_key_for(source_name: SourceName, config: dict[str, Any]) -> str:
    """Validate ``config`` for ``source_name`` and return its instance key."""
    validator = _INSTANCE_KEY_VALIDATORS[source_name]
    return validator(config)


class MonitorSourceCreateRequest(BaseModel):
    source_name: SourceName
    config: dict[str, Any]
    daily_quota: int = Field(gt=0)
    enabled: bool = True

    @model_validator(mode="after")
    def _check_config_matches_source(self) -> MonitorSourceCreateRequest:
        instance_key_for(self.source_name, self.config)
        return self


class MonitorSourceUpdateRequest(BaseModel):
    daily_quota: int | None = Field(default=None, gt=0)
    enabled: bool | None = None


class MonitorSourceResponse(BaseModel):
    id: uuid.UUID
    monitor_id: uuid.UUID
    source_name: SourceName
    instance_key: str
    config: dict[str, Any]
    daily_quota: int
    enabled: bool
    last_external_id: str | None
    last_published_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
