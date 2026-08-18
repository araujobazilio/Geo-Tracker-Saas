"""Test that SESSION_COOKIE_NAME is fully configurable.

Verifies the complete auth flow works with a non-default cookie name:
register → cookie issued under custom name → /me → /csrf → logout.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.redis import reset_redis
from app.db.session import reset_engine
from app.main import create_app

CUSTOM_COOKIE_NAME = "test_custom_session"


@pytest.fixture()
def custom_cookie_client(monkeypatch):
    """TestClient with a custom SESSION_COOKIE_NAME."""
    # Set env before importing settings.
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SESSION_COOKIE_NAME", CUSTOM_COOKIE_NAME)
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-not-for-production-use-1234567890")
    monkeypatch.setenv("REDIS_URL", os.environ.get("REDIS_URL", "redis://localhost:16379/0"))

    # Clear cached settings so the new env is picked up.
    from app.config import get_settings

    get_settings.cache_clear()
    reset_engine()
    reset_redis()

    app = create_app()
    with TestClient(app) as c:
        yield c

    reset_redis()
    get_settings.cache_clear()


@pytest.fixture()
def clean_redis():
    from app.db.redis import get_redis

    redis = get_redis()
    redis.flushdb()
    yield
    redis.flushdb()


class TestCustomCookieName:
    """Verify auth works with a non-default cookie name."""

    def test_register_uses_custom_cookie_name(self, custom_cookie_client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client = custom_cookie_client
        email = f"custom-{uuid.uuid4().hex[:8]}@example.com"
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        assert response.status_code == 201
        # Cookie should be issued under the custom name, not "geo_session".
        assert CUSTOM_COOKIE_NAME in response.cookies
        assert "geo_session" not in response.cookies

    def test_full_flow_with_custom_cookie_name(self, custom_cookie_client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client = custom_cookie_client
        email = f"flow-{uuid.uuid4().hex[:8]}@example.com"

        # Register.
        reg = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        assert reg.status_code == 201
        assert CUSTOM_COOKIE_NAME in reg.cookies

        # /me works.
        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["email"] == email

        # /csrf works.
        csrf_resp = client.get("/api/v1/auth/csrf")
        assert csrf_resp.status_code == 200
        csrf_token = csrf_resp.json()["csrf_token"]

        # Logout works.
        logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})
        assert logout.status_code == 200

        # /me after logout returns 401.
        me_after = client.get("/api/v1/auth/me")
        assert me_after.status_code == 401
