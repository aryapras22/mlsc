"""Assembles the one ``FetchClient`` every caller shares.

``FetchClient`` takes six collaborators and no production code built one —
`fetch-guardrails` left assembly to its tests. This is that assembly, so a
caller that skips it cannot end up with no circuit breaker and quietly
violate C13 (on-demand-collection design.md, "Who assembles the fetch
client").
"""

from __future__ import annotations

import curl_cffi
import httpx
from redis.asyncio import Redis

from mlsc.config import FetchClientSettings, load_fetch_client_settings
from mlsc.core.fetch.breaker import Breaker, BreakerSettings
from mlsc.core.fetch.cache import ResponseCache
from mlsc.core.fetch.client import FetchClient
from mlsc.core.fetch.throttle import HostBudget, Throttle
from mlsc.core.fetch.transports import ImpersonatingTransport, PlainTransport


def build_fetch_client(redis: Redis, settings: FetchClientSettings | None = None) -> FetchClient:
    settings = settings or load_fetch_client_settings()

    breaker = Breaker(
        redis,
        BreakerSettings(
            failure_threshold=settings.breaker_failure_threshold,
            cooldown_seconds=settings.breaker_cooldown_seconds,
        ),
    )
    cache = ResponseCache(redis, ttl_seconds=settings.cache_ttl_seconds)
    throttle = Throttle(redis)
    host_budget = HostBudget(
        capacity=settings.host_budget_capacity,
        refill_rate_per_second=settings.host_budget_refill_rate_per_second,
        jitter_low_seconds=settings.host_budget_jitter_low_seconds,
        jitter_high_seconds=settings.host_budget_jitter_high_seconds,
    )

    return FetchClient(
        breaker=breaker,
        cache=cache,
        throttle=throttle,
        plain_transport=PlainTransport(httpx.AsyncClient()),
        impersonating_transport=ImpersonatingTransport(
            curl_cffi.AsyncSession(), impersonate=settings.impersonate_profile
        ),
        host_budget=host_budget,
        max_transport_retries=settings.max_transport_retries,
        retry_backoff_seconds=settings.retry_backoff_seconds,
    )
