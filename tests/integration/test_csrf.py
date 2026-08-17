"""Integration tests for CSRF protection."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db.redis import get_redis, reset_redis
from app.db.session import reset_engine
from app.main import create_app


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


@pytest.fixture()
def authed_client(client, clean_redis):
    """Register and return a client with a valid session + CSRF token."""
    client.post(
        "/api/v1/auth/register",
        json={"email": "csrf-test@example.com", "password": "secure-password-123"},
    )
    csrf_response = client.get("/api/v1/auth/csrf")
    assert csrf_response.status_code == 200
    csrf_token = csrf_response.json()["csrf_token"]
    return client, csrf_token


class TestCSRF:
    """CSRF protection tests."""

    def test_get_does_not_require_csrf(self, authed_client) -> None:  # type: ignore[no-untyped-def]
        client, _ = authed_client
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 200

    def test_post_without_csrf_rejected(self, authed_client) -> None:  # type: ignore[no-untyped-def]
        client, _ = authed_client
        response = client.post(
            "/api/v1/workspaces",
            json={"name": "Test WS", "workspace_type": "PERSONAL"},
        )
        assert response.status_code == 403
        assert "csrf" in response.json()["error"]["code"].lower()

    def test_post_with_valid_csrf_accepted(self, authed_client) -> None:  # type: ignore[no-untyped-def]
        client, csrf_token = authed_client
        response = client.post(
            "/api/v1/workspaces",
            json={"name": "Test WS", "workspace_type": "PERSONAL"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 201

    def test_post_with_invalid_csrf_rejected(self, authed_client) -> None:  # type: ignore[no-untyped-def]
        client, _ = authed_client
        response = client.post(
            "/api/v1/workspaces",
            json={"name": "Test WS", "workspace_type": "PERSONAL"},
            headers={"X-CSRF-Token": "invalid-token-value"},
        )
        assert response.status_code == 403

    def test_post_with_wrong_session_csrf_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        # Register user A.
        client.post(
            "/api/v1/auth/register",
            json={"email": "userA@example.com", "password": "secure-password-123"},
        )
        csrf_a = client.get("/api/v1/auth/csrf").json()["csrf_token"]

        # Register user B (new session).
        client.post(
            "/api/v1/auth/register",
            json={"email": "userB@example.com", "password": "secure-password-456"},
        )
        csrf_b = client.get("/api/v1/auth/csrf").json()["csrf_token"]

        # User B's CSRF token should differ from user A's.
        assert csrf_a != csrf_b

    def test_logout_requires_csrf(self, authed_client) -> None:  # type: ignore[no-untyped-def]
        client, _ = authed_client
        # Logout without CSRF token.
        response = client.post("/api/v1/auth/logout")
        assert response.status_code == 403

    def test_logout_with_csrf_succeeds(self, authed_client) -> None:  # type: ignore[no-untyped-def]
        client, csrf_token = authed_client
        response = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200

    def test_login_exempt_from_csrf(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        # Login should work without CSRF (no session yet).
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "anyone@example.com", "password": "some-password-12"},
        )
        # Will be 401 (invalid credentials) but NOT 403 (CSRF).
        assert response.status_code == 401

    def test_register_exempt_from_csrf(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        # Register should work without CSRF (no session yet).
        response = client.post(
            "/api/v1/auth/register",
            json={"email": "csrf-exempt@example.com", "password": "secure-password-123"},
        )
        assert response.status_code == 201
