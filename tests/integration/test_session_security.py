"""Integration tests for session security."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.security import generate_session_token
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


class TestSessionTokens:
    """Session token generation and validation tests."""

    def test_session_token_is_random(self) -> None:
        token1 = generate_session_token()
        token2 = generate_session_token()
        assert token1 != token2
        assert len(token1) >= 32

    def test_token_is_hashed_in_redis(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client.post(
            "/api/v1/auth/register",
            json={"email": "hash-test@example.com", "password": "secure-password-123"},
        )
        redis = get_redis()
        # Check that no raw token is stored as a Redis key.
        keys = redis.keys("geo:session:*")
        assert len(keys) > 0
        # Keys should be hashes, not raw tokens.
        for key in keys:
            # The key suffix should be a 64-char hex SHA-256 hash.
            suffix = key.replace("geo:session:", "")
            assert len(suffix) == 64
            int(suffix, 16)  # Should be valid hex.

    def test_new_token_on_login(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        # Register.
        client.post(
            "/api/v1/auth/register",
            json={"email": "rotate@example.com", "password": "secure-password-123"},
        )
        cookie1 = client.cookies.get("geo_session")

        # Login again.
        client.post(
            "/api/v1/auth/login",
            json={"email": "rotate@example.com", "password": "secure-password-123"},
        )
        cookie2 = client.cookies.get("geo_session")

        # Session tokens should be different (session fixation protection).
        assert cookie1 != cookie2

    def test_revoked_token_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client.post(
            "/api/v1/auth/register",
            json={"email": "revoke@example.com", "password": "secure-password-123"},
        )
        # Logout revokes the session.
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        # Should not be authenticated.
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401

    def test_tampered_session_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client.post(
            "/api/v1/auth/register",
            json={"email": "tamper@example.com", "password": "secure-password-123"},
        )
        # Tamper with the cookie.
        client.cookies.set("geo_session", "tampered-token-value")
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401

    def test_unknown_session_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        # Set a random session cookie without registering.
        client.cookies.set("geo_session", generate_session_token())
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401

    def test_inactive_user_rejected_with_valid_session(
        self, client, clean_redis, db_session
    ) -> None:  # type: ignore[no-untyped-def]
        # Register and get a valid session.
        client.post(
            "/api/v1/auth/register",
            json={"email": "deactivate@example.com", "password": "secure-password-123"},
        )
        assert client.get("/api/v1/auth/me").status_code == 200

        # Deactivate the user directly in the DB.
        from app.models.user import User

        user = db_session.query(User).filter_by(email="deactivate@example.com").first()
        assert user is not None
        user.is_active = False
        db_session.commit()

        # Session should no longer grant access.
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401
