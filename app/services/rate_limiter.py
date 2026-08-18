"""Redis-backed rate limiter for authentication endpoints.

Provides lightweight, configurable **fixed-window** rate limiting for
login and register endpoints. A Redis counter is incremented per key
(IP or email); the TTL is set only on the first increment so the
window is fixed, not sliding.

Policy:
  - login: 8 failed attempts per IP per 5-minute window
  - register: 5 attempts per IP per 1-hour window

Login semantics:
  - `is_limited()` is checked BEFORE authentication.
  - `record_failure()` is called only when authentication FAILS.
  - `reset()` is called when authentication SUCCEEDS, clearing any
    prior failure count so successful logins do not consume quota.

Registration is request-based (each request increments) rather than
failure-based.

Rate limiting does NOT permanently lock users. It is a throttling
mechanism to slow down brute-force attempts.
"""

from __future__ import annotations

from redis import Redis

from app.db.redis import get_redis

_RATE_PREFIX = "geo:ratelimit:"


class RateLimiter:
    """Fixed-window rate limiter backed by Redis.

    The counter increments on each call to `record_failure()` (for login)
    or `check()` (for register). The TTL is set only when the counter
    is first created (count == 1), ensuring a fixed window that does not
    extend on subsequent attempts.
    """

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

    def is_limited(self, scope: str, identifier: str) -> bool:
        """Return True if the identifier is currently rate-limited.

        Does NOT increment the counter. Use this to check before
        attempting authentication.
        """
        key = self._key(scope, identifier)
        count = self.redis.get(key)
        if count is None:
            return False
        return int(count) >= self._max

    def record_failure(self, scope: str, identifier: str) -> int:
        """Increment the failure counter for an identifier.

        Sets the TTL only on the first increment (count == 1) so the
        window is fixed. Returns the new count.
        """
        key = self._key(scope, identifier)
        pipe = self.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, self._window, nx=True)  # nx=True: only set if no TTL
        count, _ = pipe.execute()
        return int(count)

    def check(self, scope: str, identifier: str) -> bool:
        """Increment counter and return True if within limit.

        Used for request-based rate limiting (e.g. register). Sets TTL
        only on the first increment.
        """
        key = self._key(scope, identifier)
        pipe = self.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, self._window, nx=True)
        count, _ = pipe.execute()
        return int(count) <= self._max

    def get_count(self, scope: str, identifier: str) -> int:
        """Return the current counter value (0 if not set)."""
        key = self._key(scope, identifier)
        count = self.redis.get(key)
        return int(count) if count is not None else 0

    def get_ttl(self, scope: str, identifier: str) -> int:
        """Return the remaining TTL in seconds (-1 if no TTL, -2 if key missing)."""
        return int(self.redis.ttl(self._key(scope, identifier)))

    def reset(self, scope: str, identifier: str) -> None:
        """Reset the counter for an identifier (e.g. on successful login)."""
        self.redis.delete(self._key(scope, identifier))
