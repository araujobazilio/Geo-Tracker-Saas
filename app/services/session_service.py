"""Session service — opaque server-side sessions stored in Redis.

Architecture:
    Browser → HttpOnly cookie (opaque token) → Redis session record → user_id

The browser cookie contains only a random opaque session token.
Redis stores session data (user_id, created_at, expires_at, csrf_token)
under a SHA-256 hash of the token, never the raw token itself.

Session data in Redis is NOT treated as authorization truth for
workspace membership. Workspace authorization always uses the
authoritative database membership tables.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from redis import Redis

from app.core.security import generate_csrf_token, generate_session_token, hash_token, safe_eq
from app.db.redis import get_redis

# Redis key namespace.
_SESSION_PREFIX = "geo:session:"
# Index: user_id → set of session key hashes (for "revoke all sessions").
_USER_SESSIONS_PREFIX = "geo:user_sessions:"


@dataclass(frozen=True)
class SessionData:
    """Session record stored in Redis (and returned to callers)."""

    user_id: str
    csrf_token: str
    created_at: float
    expires_at: float

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def to_json(self) -> str:
        return json.dumps(
            {
                "user_id": self.user_id,
                "csrf_token": self.csrf_token,
                "created_at": self.created_at,
                "expires_at": self.expires_at,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> SessionData:
        data = json.loads(raw)
        return cls(
            user_id=data["user_id"],
            csrf_token=data["csrf_token"],
            created_at=data["created_at"],
            expires_at=data["expires_at"],
        )


class SessionService:
    """Manage opaque server-side sessions in Redis."""

    def __init__(self, redis: Redis[str] | None = None, ttl_seconds: int = 7 * 24 * 3600) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @property
    def redis(self) -> Redis[str]:
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    def _session_key(self, token: str) -> str:
        return f"{_SESSION_PREFIX}{hash_token(token)}"

    def _user_sessions_key(self, user_id: str) -> str:
        return f"{_USER_SESSIONS_PREFIX}{user_id}"

    def create_session(self, user_id: str) -> tuple[str, SessionData]:
        """Create a new session for `user_id`.

        Returns (raw_token, session_data). The raw token is given to the
        browser; only its hash is stored in Redis.
        """
        token = generate_session_token()
        csrf_token = generate_csrf_token()
        now = time.time()
        expires_at = now + self._ttl
        session = SessionData(
            user_id=str(user_id),
            csrf_token=csrf_token,
            created_at=now,
            expires_at=expires_at,
        )
        key = self._session_key(token)
        self.redis.setex(key, self._ttl, session.to_json())
        # Index for bulk revocation.
        self.redis.sadd(self._user_sessions_key(str(user_id)), key)
        self.redis.expire(self._user_sessions_key(str(user_id)), self._ttl)
        return token, session

    def get_session(self, token: str) -> SessionData | None:
        """Retrieve and validate a session by raw token.

        Returns None if the session does not exist or has expired.
        """
        if not token:
            return None
        key = self._session_key(token)
        raw = self.redis.get(key)
        if raw is None:
            return None
        session = SessionData.from_json(raw)
        if session.is_expired:
            self.redis.delete(key)
            return None
        return session

    def revoke_session(self, token: str) -> None:
        """Revoke (delete) a session by raw token. Idempotent."""
        if not token:
            return
        key = self._session_key(token)
        raw = self.redis.get(key)
        if raw is not None:
            session = SessionData.from_json(raw)
            self.redis.srem(self._user_sessions_key(session.user_id), key)
        self.redis.delete(key)

    def revoke_all_user_sessions(self, user_id: str) -> None:
        """Revoke all sessions for a user (e.g. 'log out all devices')."""
        user_key = self._user_sessions_key(str(user_id))
        keys = self.redis.smembers(user_key)
        for key in keys:
            self.redis.delete(key)
        self.redis.delete(user_key)

    def validate_csrf(self, token: str, csrf_token: str) -> bool:
        """Validate a CSRF token against the session's stored token.

        Uses constant-time comparison.
        """
        session = self.get_session(token)
        if session is None:
            return False
        return safe_eq(session.csrf_token, csrf_token)
