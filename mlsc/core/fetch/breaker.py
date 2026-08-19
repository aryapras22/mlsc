"""Redis-backed per-host circuit breaker.

Counts only transport failures — a validation failure means the host answered,
so it says nothing about whether the host is reachable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis

from mlsc.core.fetch.contracts import BreakerOpen


class Clock(Protocol):
    def time(self) -> float: ...


@dataclass(frozen=True)
class BreakerSettings:
    """Trip after this many consecutive transport failures; cool for this long."""

    failure_threshold: int
    cooldown_seconds: float


class Breaker:
    """One breaker state per host, shared across worker processes via Redis."""

    def __init__(
        self,
        redis: Redis,
        settings: BreakerSettings,
        *,
        clock: Clock | None = None,
        key_prefix: str = "mlsc:fetch:breaker",
    ) -> None:
        self._redis = redis
        self._settings = settings
        self._clock = clock or _SystemClock()
        self._key_prefix = key_prefix

    async def check(self, host_key: str) -> None:
        """Raise ``BreakerOpen`` if this host is still cooling; otherwise return."""
        opened_until = await self._redis.get(self._opened_until_key(host_key))
        if opened_until is None:
            return
        opened_until_value = float(opened_until)
        if self._clock.time() < opened_until_value:
            raise BreakerOpen(host_key=host_key, opened_until=opened_until_value)
        await self._redis.delete(self._opened_until_key(host_key), self._failures_key(host_key))

    async def record_success(self, host_key: str) -> None:
        await self._redis.delete(self._failures_key(host_key), self._opened_until_key(host_key))

    async def record_failure(self, host_key: str) -> None:
        """Increment the consecutive-failure count and open the breaker past the threshold."""
        failures = await self._redis.incr(self._failures_key(host_key))
        if failures >= self._settings.failure_threshold:
            opened_until = self._clock.time() + self._settings.cooldown_seconds
            await self._redis.set(self._opened_until_key(host_key), opened_until)

    def _failures_key(self, host_key: str) -> str:
        return f"{self._key_prefix}:{host_key}:failures"

    def _opened_until_key(self, host_key: str) -> str:
        return f"{self._key_prefix}:{host_key}:opened_until"


class _SystemClock:
    """Wall-clock time: breaker state is shared across worker processes via Redis."""

    def time(self) -> float:
        import time

        return time.time()
