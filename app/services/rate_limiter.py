"""Redis-backed rate limiter for authentication endpoints.

Provides lightweight, configurable rate limiting for login and register
endpoints. Uses a sliding window counter per key (IP or email).

Policy:
  - login: 8 failed attempts per IP per 5-minute window
  - register: 5 attempts per IP per 1-hour window

Rate limiting does NOT permanently lock users. It is a throttling
mechanism to slow down brute-force attempts.
"""

from __future__ import annotations

from redis import Redis

from app.db.redis import get_redis

_RATE_PREFIX = "geo:ratelimit:"


class RateLimiter:
    """Sliding-window rate limiter backed by Redis."""

    def __init__(
        self,
        redis: Redis[str] | None = None,
        max_attempts: int = 8,
        window_seconds: int = 300,
    ) -> None:
        self._redis = redis
        self._max = max_attempts
        self._window = window_seconds

    @property
    def redis(self) -> Redis[str]:
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    def _key(self, scope: str, identifier: str) -> str:
        return f"{_RATE_PREFIX}{scope}:{identifier}"

    def check(self, scope: str, identifier: str) -> bool:
        """Return True if the request is within the rate limit.

        Increments the counter. If the count exceeds the limit, returns
        False (request should be denied).
        """
        key = self._key(scope, identifier)
        pipe = self.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, self._window)
        count, _ = pipe.execute()
        return int(count) <= self._max

    def is_limited(self, scope: str, identifier: str) -> bool:
        """Return True if the identifier is currently rate-limited (without incrementing)."""
        key = self._key(scope, identifier)
        count = self.redis.get(key)
        if count is None:
            return False
        return int(count) > self._max

    def reset(self, scope: str, identifier: str) -> None:
        """Reset the counter for an identifier (e.g. on successful login)."""
        self.redis.delete(self._key(scope, identifier))
