"""Response cache keyed by content hash and prompt version.

Falls back to a live call on any read/write error — the cache is an
optimisation and must never be load-bearing for correctness (design.md,
"Failure strategy").
"""

from __future__ import annotations

import json
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class LlmResponseCache:
    def __init__(self, redis: Redis, *, key_prefix: str = "mlsc:llm:cache") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    async def get(self, content_hash: str, prompt_version: str) -> dict | None:
        try:
            raw = await self._redis.get(self._key_for(content_hash, prompt_version))
        except RedisError:
            logger.warning("LLM cache read failed; falling back to a live call")
            return None
        return json.loads(raw) if raw else None

    async def put(self, content_hash: str, prompt_version: str, value: dict) -> None:
        try:
            await self._redis.set(
                self._key_for(content_hash, prompt_version), json.dumps(value), ex=86400 * 30
            )
        except RedisError:
            logger.warning("LLM cache write failed; result was not cached")

    def _key_for(self, content_hash: str, prompt_version: str) -> str:
        return f"{self._key_prefix}:{content_hash}:{prompt_version}"
