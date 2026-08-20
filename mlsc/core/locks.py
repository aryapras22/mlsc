"""Token-fenced Redis lock: only the holder that acquired it can release it.

A stale worker cannot release a lock retaken by someone else after its TTL
expired, because release checks the token rather than just deleting the key.
"""

from __future__ import annotations

import uuid

from redis.asyncio import Redis

_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
else
  return 0
end
"""


class LockLost(RuntimeError):
    """Raised when release is attempted with a token that no longer holds the lock."""


class RunLock:
    def __init__(self, redis: Redis, *, key_prefix: str = "mlsc:run:lock") -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    async def acquire(self, key: str, *, ttl_seconds: int) -> str | None:
        """Return a token if the lock was free, else ``None``."""
        token = str(uuid.uuid4())
        acquired = await self._redis.set(
            f"{self._key_prefix}:{key}", token, nx=True, ex=ttl_seconds
        )
        return token if acquired else None

    async def current_token(self, key: str) -> str | None:
        value = await self._redis.get(f"{self._key_prefix}:{key}")
        return value.decode() if isinstance(value, bytes) else value

    async def release(self, key: str, token: str) -> None:
        result = await self._redis.eval(_RELEASE_SCRIPT, 1, f"{self._key_prefix}:{key}", token)
        if not result:
            raise LockLost(key)
