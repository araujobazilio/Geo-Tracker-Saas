"""Redis client management.

Provides a lazily-initialized Redis client and a connectivity probe
used by the /ready endpoint.
"""

from __future__ import annotations

import contextlib

from redis import Redis
from redis.exceptions import RedisError

from app.config import get_settings

_redis: Redis[str] | None = None


def get_redis() -> Redis[str]:
    """Return the lazily-initialized global Redis client."""
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
    return _redis


def check_redis() -> bool:
    """Return True if Redis PING succeeds."""
    try:
        return bool(get_redis().ping())
    except RedisError:
        return False
    except Exception:
        return False


def reset_redis() -> None:
    """Reset cached client (used in tests)."""
    global _redis
    if _redis is not None:
        with contextlib.suppress(Exception):
            _redis.close()
    _redis = None
