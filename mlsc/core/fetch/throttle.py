"""Redis-backed per-host token bucket with centred jitter.

State lives in Redis so two worker processes throttling the same host share one
budget instead of each enforcing its own.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis


class Clock(Protocol):
    """Time source and sleeper, injected so throttling is deterministic under test."""

    def time(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class RandomSource(Protocol):
    """Injected so jitter is deterministic under test."""

    def uniform(self, low: float, high: float) -> float: ...


@dataclass(frozen=True)
class HostBudget:
    """One host's rate limit: burst capacity, steady refill, and jitter spread."""

    capacity: float
    refill_rate_per_second: float
    jitter_low_seconds: float
    jitter_high_seconds: float


class SystemClock:
    """The real clock and sleeper, used everywhere except tests.

    Uses wall-clock time rather than a monotonic clock: bucket state is shared
    across worker processes via Redis, and each process's monotonic clock has
    its own, incomparable epoch.
    """

    def time(self) -> float:
        import time

        return time.time()

    async def sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)


class Throttle:
    """Paces requests to one host to its own budget, independent of every other host."""

    def __init__(
        self,
        redis: Redis,
        *,
        clock: Clock | None = None,
        random_source: RandomSource | None = None,
        key_prefix: str = "mlsc:fetch:throttle",
    ) -> None:
        self._redis = redis
        self._clock = clock or SystemClock()
        self._random = random_source or random.Random()
        self._key_prefix = key_prefix

    async def acquire(self, host_key: str, budget: HostBudget) -> None:
        """Block until this host has a token, then add a jittered gap before returning."""
        while True:
            wait_seconds = await self._take_token(host_key, budget)
            if wait_seconds <= 0:
                break
            await self._clock.sleep(wait_seconds)

        jitter = self._random.uniform(budget.jitter_low_seconds, budget.jitter_high_seconds)
        if jitter > 0:
            await self._clock.sleep(jitter)

    async def _take_token(self, host_key: str, budget: HostBudget) -> float:
        """Refill by elapsed time, take one token if available, and persist the result.

        Read-then-write against Redis rather than a Lua script: an occasional race
        between two workers costs at most one extra token, which is acceptable for
        politeness pacing and keeps this testable with plain get/set fakes.
        """
        tokens_key = f"{self._key_prefix}:{host_key}:tokens"
        updated_key = f"{self._key_prefix}:{host_key}:updated_at"

        now = self._clock.time()
        raw_tokens = await self._redis.get(tokens_key)
        raw_updated = await self._redis.get(updated_key)

        if raw_tokens is None or raw_updated is None:
            tokens = budget.capacity
        else:
            elapsed = max(0.0, now - float(raw_updated))
            tokens = min(budget.capacity, float(raw_tokens) + elapsed * budget.refill_rate_per_second)

        if tokens >= 1:
            tokens -= 1
            wait_seconds = 0.0
        else:
            wait_seconds = (1 - tokens) / budget.refill_rate_per_second

        await self._redis.set(tokens_key, str(tokens))
        await self._redis.set(updated_key, str(now))
        return wait_seconds
