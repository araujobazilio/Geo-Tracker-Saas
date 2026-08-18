"""Tests for Redis-backed rate limiting.

Tests are deterministic — no real waiting/sleep is required.
Uses a real Redis instance with unique identifiers per test.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.redis import get_redis, reset_redis
from app.db.session import reset_engine
from app.main import create_app
from app.services.rate_limiter import RateLimiter


def _unique_id() -> str:
    return f"test-ip-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def client():
    reset_engine()
    reset_redis()
    from app.config import get_settings

    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_redis()


@pytest.fixture()
def clean_redis():
    redis = get_redis()
    redis.flushdb()
    yield
    redis.flushdb()


@pytest.fixture()
def rate_limiter(clean_redis):
    """Fresh rate limiter with low limits for testing."""
    return RateLimiter(redis=get_redis(), max_attempts=3, window_seconds=300)


class TestRateLimiterAlgorithm:
    """Unit tests for the fixed-window rate limiter."""

    def test_record_failure_increments(self, rate_limiter) -> None:  # type: ignore[no-untyped-def]
        ip = _unique_id()
        assert rate_limiter.record_failure("login", ip) == 1
        assert rate_limiter.record_failure("login", ip) == 2
        assert rate_limiter.record_failure("login", ip) == 3

    def test_is_limited_false_initially(self, rate_limiter) -> None:  # type: ignore[no-untyped-def]
        ip = _unique_id()
        assert rate_limiter.is_limited("login", ip) is False

    def test_is_limited_true_after_threshold(self, rate_limiter) -> None:  # type: ignore[no-untyped-def]
        ip = _unique_id()
        for _ in range(3):
            rate_limiter.record_failure("login", ip)
        assert rate_limiter.is_limited("login", ip) is True

    def test_is_limited_false_below_threshold(self, rate_limiter) -> None:  # type: ignore[no-untyped-def]
        ip = _unique_id()
        rate_limiter.record_failure("login", ip)
        rate_limiter.record_failure("login", ip)
        assert rate_limiter.is_limited("login", ip) is False

    def test_reset_clears_counter(self, rate_limiter) -> None:  # type: ignore[no-untyped-def]
        ip = _unique_id()
        rate_limiter.record_failure("login", ip)
        rate_limiter.record_failure("login", ip)
        rate_limiter.reset("login", ip)
        assert rate_limiter.get_count("login", ip) == 0
        assert rate_limiter.is_limited("login", ip) is False

    def test_ttl_set_only_on_first_increment(self, rate_limiter) -> None:  # type: ignore[no-untyped-def]
        """Fixed-window: TTL should not be refreshed on subsequent increments."""
        ip = _unique_id()
        rate_limiter.record_failure("login", ip)
        ttl1 = rate_limiter.get_ttl("login", ip)
        assert ttl1 > 0  # TTL was set

        rate_limiter.record_failure("login", ip)
        rate_limiter.record_failure("login", ip)
        ttl2 = rate_limiter.get_ttl("login", ip)
        # TTL should be <= ttl1 (not refreshed/extended).
        assert ttl2 <= ttl1

    def test_check_increments_for_register(self, rate_limiter) -> None:  # type: ignore[no-untyped-def]
        ip = _unique_id()
        assert rate_limiter.check("register", ip) is True  # count=1
        assert rate_limiter.check("register", ip) is True  # count=2
        assert rate_limiter.check("register", ip) is True  # count=3
        assert rate_limiter.check("register", ip) is False  # count=4 > max

    def test_scopes_are_independent(self, rate_limiter) -> None:  # type: ignore[no-untyped-def]
        ip = _unique_id()
        for _ in range(3):
            rate_limiter.record_failure("login", ip)
        assert rate_limiter.is_limited("login", ip) is True
        # Register scope should be unaffected.
        assert rate_limiter.is_limited("register", ip) is False


class TestLoginRateLimitingIntegration:
    """Integration tests for login rate limiting via the API."""

    def test_successful_login_does_not_consume_quota(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        email = f"rl-success-{uuid.uuid4().hex[:8]}@example.com"
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        # Logout to clear session.
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        # Login successfully multiple times — should never be limited.
        for _ in range(10):
            resp = client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "secure-password-123"},
            )
            assert resp.status_code == 200

    def test_failed_login_increments_counter(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        email = f"rl-fail-{uuid.uuid4().hex[:8]}@example.com"
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        # Logout.
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        # Failed login attempts.
        for _ in range(7):
            resp = client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "wrong-password-99"},
            )
            assert resp.status_code == 401  # Not rate-limited yet (under 8).

        # 8th failure should trigger rate limit (429).
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "wrong-password-99"},
        )
        assert resp.status_code == 429

    def test_successful_login_resets_failure_counter(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        email = f"rl-reset-{uuid.uuid4().hex[:8]}@example.com"
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        # Logout.
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        # Accumulate some failures.
        for _ in range(5):
            client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "wrong-password-99"},
            )

        # Now login successfully — should reset counter.
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "secure-password-123"},
        )
        assert resp.status_code == 200

        # Logout and verify we can fail again (counter was reset).
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        for _ in range(7):
            resp = client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "wrong-password-99"},
            )
            assert resp.status_code == 401  # Not limited yet.

    def test_rate_limited_login_returns_429(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        email = f"rl-block-{uuid.uuid4().hex[:8]}@example.com"
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        # Exhaust the limit.
        for _ in range(8):
            client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "wrong-password-99"},
            )

        # Even a correct password should be blocked now.
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "secure-password-123"},
        )
        assert resp.status_code == 429
