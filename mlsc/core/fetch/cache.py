"""Request-keyed response cache.

Keyed by request and collection window so a retry elsewhere in the same day's
run is served from the earlier response rather than refetching. A cache error
degrades to a live fetch — the cache is an optimisation, its failure must not
fail a fetch.
"""

from __future__ import annotations

import hashlib
import json
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from mlsc.core.fetch.contracts import FetchOutcome, FetchRequest, FetchStatus

logger = logging.getLogger(__name__)


class ResponseCache:
    """Caches successful outcomes only; failures are never worth replaying."""

    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int,
        key_prefix: str = "mlsc:fetch:cache",
    ) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._key_prefix = key_prefix

    async def lookup(self, request: FetchRequest) -> FetchOutcome | None:
        try:
            raw = await self._redis.get(self._key_for(request))
        except RedisError:
            logger.warning("fetch cache lookup failed for %s; falling back to live fetch", request.host_key)
            return None
        if raw is None:
            return None
        decoded = json.loads(raw)
        return FetchOutcome(
            status=FetchStatus(decoded["status"]),
            payload=decoded["payload"],
            served_from_cache=True,
            library_version=decoded["library_version"],
            duration_seconds=decoded["duration_seconds"],
        )

    async def store(self, request: FetchRequest, outcome: FetchOutcome) -> None:
        if outcome.status is not FetchStatus.OK:
            return
        encoded = json.dumps(
            {
                "status": outcome.status.value,
                "payload": outcome.payload,
                "library_version": outcome.library_version,
                "duration_seconds": outcome.duration_seconds,
            }
        )
        try:
            await self._redis.set(self._key_for(request), encoded, ex=self._ttl_seconds)
        except RedisError:
            logger.warning("fetch cache store failed for %s; result was not cached", request.host_key)

    def _key_for(self, request: FetchRequest) -> str:
        digest = hashlib.sha256(
            json.dumps(
                {
                    "url": request.url,
                    "method": request.method,
                    "query": request.query,
                    "collection_window": request.collection_window,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return f"{self._key_prefix}:{digest}"
