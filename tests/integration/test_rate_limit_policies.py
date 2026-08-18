"""Tests for separate login and registration rate limit policies.

Verifies that:
  - get_login_rate_limiter uses LOGIN config values.
  - get_register_rate_limiter uses REGISTER config values.
  - The two policies are independent (different max/window).
  - Register endpoint obeys REGISTER max, not LOGIN max.
  - Login endpoint obeys LOGIN max, not REGISTER max.
  - Redis key namespaces are isolated (login vs register).
  - The test can fail if policies are accidentally swapped.

All tests are deterministic — no sleep or wall-clock dependency.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.redis import get_redis, reset_redis
from app.db.session import reset_engine
from app.dependencies import get_login_rate_limiter, get_register_rate_limiter
from app.main import create_app
from app.services.rate_limiter import RateLimiter


def _unique_email() -> str:
    return f"sep-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture()
def client():
    reset_engine()
    reset_redis()
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


class TestRateLimiterDependencyConfiguration:
    """Verify the two dependencies use different configured values."""

    def test_login_limiter_uses_login_config(self) -> None:  # type: ignore[no-untyped-def]
        settings = get_settings()
        limiter = get_login_rate_limiter(settings)
        assert limiter._max == settings.rate_limit_login_max
        assert limiter._window == settings.rate_limit_login_window_seconds

    def test_register_limiter_uses_register_config(self) -> None:  # type: ignore[no-untyped-def]
        settings = get_settings()
        limiter = get_register_rate_limiter(settings)
        assert limiter._max == settings.rate_limit_register_max
        assert limiter._window == settings.rate_limit_register_window_seconds

    def test_login_and_register_limiters_differ(self) -> None:  # type: ignore[no-untyped-def]
        settings = get_settings()
        login_limiter = get_login_rate_limiter(settings)
        register_limiter = get_register_rate_limiter(settings)
        # The default config has different values for login vs register.
        assert login_limiter._max != register_limiter._max or (
            login_limiter._window != register_limiter._window
        )


class TestRedisKeyIsolation:
    """Verify login and register counters use separate Redis keys."""

    def test_login_failures_do_not_affect_register_counter(self, clean_redis) -> None:  # type: ignore[no-untyped-def]
        redis = get_redis()
        login_limiter = RateLimiter(redis=redis, max_attempts=3, window_seconds=300)
        register_limiter = RateLimiter(redis=redis, max_attempts=3, window_seconds=300)
        ip = "10.0.0.1"

        # Record login failures.
        login_limiter.record_failure("login", ip)
        login_limiter.record_failure("login", ip)

        # Register counter should be zero.
        assert register_limiter.get_count("register", ip) == 0
        assert register_limiter.is_limited("register", ip) is False

    def test_register_requests_do_not_affect_login_counter(self, clean_redis) -> None:  # type: ignore[no-untyped-def]
        redis = get_redis()
        login_limiter = RateLimiter(redis=redis, max_attempts=3, window_seconds=300)
        register_limiter = RateLimiter(redis=redis, max_attempts=3, window_seconds=300)
        ip = "10.0.0.2"

        # Register requests.
        register_limiter.check("register", ip)
        register_limiter.check("register", ip)

        # Login counter should be zero.
        assert login_limiter.get_count("login", ip) == 0
        assert login_limiter.is_limited("login", ip) is False


class TestEndpointRateLimitPolicies:
    """Integration tests proving endpoints use the correct policy.

    Uses environment overrides to set intentionally different limits:
      LOGIN: max=2, window=60
      REGISTER: max=4, window=600

    This makes the test capable of failing if policies are swapped.
    """

    @pytest.fixture()
    def custom_client(self, monkeypatch):
        """Client with small, distinct rate limits for login and register."""
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("APP_SECRET_KEY", "test-secret-not-for-production-use-1234567890")
        monkeypatch.setenv("REDIS_URL", os.environ.get("REDIS_URL", "redis://localhost:16379/0"))
        monkeypatch.setenv("RATE_LIMIT_LOGIN_MAX", "2")
        monkeypatch.setenv("RATE_LIMIT_LOGIN_WINDOW_SECONDS", "60")
        monkeypatch.setenv("RATE_LIMIT_REGISTER_MAX", "4")
        monkeypatch.setenv("RATE_LIMIT_REGISTER_WINDOW_SECONDS", "600")

        get_settings.cache_clear()
        reset_engine()
        reset_redis()

        app = create_app()
        with TestClient(app) as c:
            yield c

        reset_redis()
        get_settings.cache_clear()

    def test_register_obey_register_max_not_login_max(self, custom_client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        """Register allows up to REGISTER_MAX (4) requests, not LOGIN_MAX (2)."""
        client = custom_client
        # First 4 registrations should succeed (request-based, max=4).
        for i in range(4):
            resp = client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"reg-{i}-{uuid.uuid4().hex[:8]}@example.com",
                    "password": "secure-password-123",
                },
            )
            assert resp.status_code == 201, f"Request {i + 1} should succeed: {resp.text}"

        # 5th registration should be rate-limited (exceeds max=4).
        resp = client.post(
            "/api/v1/auth/register",
            json={
                "email": f"reg-5-{uuid.uuid4().hex[:8]}@example.com",
                "password": "secure-password-123",
            },
        )
        assert resp.status_code == 429

    def test_login_obey_login_max_not_register_max(self, custom_client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        """Login allows up to LOGIN_MAX (2) failures before blocking.

        With max=2: the 1st failure returns 401 (count=1, not limited).
        The 2nd failure increments to count=2 which triggers 429.
        If swapped to register policy (max=4), we'd get more 401s.
        """
        client = custom_client
        # Register a user first.
        email = f"login-test-{uuid.uuid4().hex[:8]}@example.com"
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        # Logout to clear session.
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        # 1st failure: count=1, not limited -> 401.
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "wrong-password-99"},
        )
        assert resp.status_code == 401, f"1st failure should return 401: {resp.text}"

        # 2nd failure: count=2, limited -> 429.
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "wrong-password-99"},
        )
        assert resp.status_code == 429, "2nd failure should trigger 429 with max=2"

    def test_policies_are_not_swapped(self, custom_client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        """If policies were swapped, register would block at 2 and login at 4.

        This test verifies that register allows >2 (LOGIN_MAX) requests
        and login blocks at 2 failures (well below REGISTER_MAX=4).
        """
        client = custom_client

        # Register 3 users — if swapped to login policy (max=2), 3rd would fail.
        for i in range(3):
            resp = client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"swap-reg-{i}-{uuid.uuid4().hex[:8]}@example.com",
                    "password": "secure-password-123",
                },
            )
            assert resp.status_code == 201, f"Register {i + 1} should succeed if not swapped"

        # Now test login: 2nd failure should block (max=2).
        # If swapped to register policy (max=4), 2nd would still be 401, not 429.
        email = f"swap-login-{uuid.uuid4().hex[:8]}@example.com"
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        # 1st failure: count=1, not limited -> 401.
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "wrong-password-99"},
        )
        assert resp.status_code == 401

        # 2nd failure: count=2, limited -> 429.
        # If swapped to register policy (max=4), this would be 401 instead.
        resp = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "wrong-password-99"},
        )
        assert resp.status_code == 429, "Login should block at max=2, not allow up to 4"
