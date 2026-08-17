"""Integration tests for authentication endpoints.

Tests registration, login, logout, /me, cookie security, session
management, and email normalization.

Each test uses a unique email to avoid conflicts with other tests,
since TestClient-based tests commit real data to the test database.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.redis import get_redis, reset_redis
from app.db.session import reset_engine
from app.main import create_app
from app.models.user import User

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


def _unique_email() -> str:
    return f"test-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture()
def client():
    """FastAPI test client with fresh app state."""
    reset_engine()
    reset_redis()
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_redis()


@pytest.fixture()
def clean_redis():
    """Flush Redis before each test to ensure clean session state."""
    redis = get_redis()
    redis.flushdb()
    yield
    redis.flushdb()


def _direct_db_session() -> Session:
    """Create a direct DB session that commits are visible to the app."""
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = factory()
    return session


@pytest.fixture()
def registered_user(client, clean_redis):
    """Register a test user and return the response data + client."""
    email = _unique_email()
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "secure-password-123"},
    )
    assert response.status_code == 201, response.text
    return response


class TestRegistration:
    """Registration endpoint tests."""

    def test_register_success(self, registered_user) -> None:  # type: ignore[no-untyped-def]
        assert registered_user.status_code == 201
        data = registered_user.json()
        assert "id" in data
        assert data["email"].endswith("@example.com")
        assert data["is_admin"] is False
        assert "password_hash" not in data

    def test_register_creates_default_workspace(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        email = _unique_email()
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        assert response.status_code == 201
        me_response = client.get("/api/v1/auth/me")
        assert me_response.status_code == 200
        me_data = me_response.json()
        assert len(me_data["workspaces"]) == 1
        assert me_data["workspaces"][0]["name"] == "My Workspace"
        assert me_data["workspaces"][0]["role"] == "OWNER"

    def test_register_sets_session_cookie(self, registered_user) -> None:  # type: ignore[no-untyped-def]
        cookies = registered_user.cookies
        settings = get_settings()
        assert settings.session_cookie_name in cookies

    def test_register_cookie_is_httponly(self, registered_user) -> None:  # type: ignore[no-untyped-def]
        raw_cookie = registered_user.headers.get("set-cookie", "")
        assert "httponly" in raw_cookie.lower()

    def test_register_cookie_samesite_lax(self, registered_user) -> None:  # type: ignore[no-untyped-def]
        raw_cookie = registered_user.headers.get("set-cookie", "")
        assert "samesite=lax" in raw_cookie.lower()

    def test_register_cookie_has_path(self, registered_user) -> None:  # type: ignore[no-untyped-def]
        raw_cookie = registered_user.headers.get("set-cookie", "")
        assert "path=/" in raw_cookie.lower()

    def test_register_cookie_has_max_age(self, registered_user) -> None:  # type: ignore[no-untyped-def]
        raw_cookie = registered_user.headers.get("set-cookie", "")
        assert "max-age=" in raw_cookie.lower()

    def test_duplicate_email_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        email = _unique_email()
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-456"},
        )
        assert response.status_code == 409

    def test_case_variant_duplicate_email_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        email = _unique_email()
        local, domain = email.split("@")
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        # Use uppercase variant.
        upper_email = f"{local.capitalize()}@{domain.capitalize()}"
        response = client.post(
            "/api/v1/auth/register",
            json={"email": upper_email, "password": "secure-password-456"},
        )
        assert response.status_code == 409

    def test_short_password_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        response = client.post(
            "/api/v1/auth/register",
            json={"email": _unique_email(), "password": "short123"},
        )
        assert response.status_code == 422

    def test_invalid_email_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        response = client.post(
            "/api/v1/auth/register",
            json={"email": "not-an-email", "password": "secure-password-123"},
        )
        assert response.status_code == 422

    def test_register_does_not_return_password_hash(self, registered_user) -> None:  # type: ignore[no-untyped-def]
        data = registered_user.json()
        assert "password_hash" not in str(data)


class TestLogin:
    """Login endpoint tests."""

    def test_login_success(self, client, clean_redis, registered_user) -> None:  # type: ignore[no-untyped-def]
        email = registered_user.json()["email"]
        response = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "secure-password-123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["email"] == email

    def test_login_wrong_password(self, client, clean_redis, registered_user) -> None:  # type: ignore[no-untyped-def]
        email = registered_user.json()["email"]
        response = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "wrong-password-999"},
        )
        assert response.status_code == 401
        assert "Invalid email or password" in response.json()["error"]["message"]

    def test_login_nonexistent_user_generic_error(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        response = client.post(
            "/api/v1/auth/login",
            json={"email": _unique_email(), "password": "some-password-123"},
        )
        assert response.status_code == 401
        assert "Invalid email or password" in response.json()["error"]["message"]

    def test_login_inactive_user_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        email = _unique_email()
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        # Deactivate user via direct DB session (commits are visible to app).
        session = _direct_db_session()
        try:
            session.execute(update(User).where(User.email == email).values(is_active=False))
            session.commit()
        finally:
            session.close()

        response = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "secure-password-123"},
        )
        assert response.status_code == 401

    def test_login_sets_session_cookie(self, client, clean_redis, registered_user) -> None:  # type: ignore[no-untyped-def]
        email = registered_user.json()["email"]
        response = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "secure-password-123"},
        )
        settings = get_settings()
        assert settings.session_cookie_name in response.cookies

    def test_login_normalizes_email(self, client, clean_redis, registered_user) -> None:  # type: ignore[no-untyped-def]
        email = registered_user.json()["email"]
        local, domain = email.split("@")
        # Login with uppercase email should work.
        upper_email = f"{local.capitalize()}@{domain.capitalize()}"
        response = client.post(
            "/api/v1/auth/login",
            json={"email": upper_email, "password": "secure-password-123"},
        )
        assert response.status_code == 200


class TestLogout:
    """Logout endpoint tests."""

    def test_logout_revokes_session(self, client, clean_redis, registered_user) -> None:  # type: ignore[no-untyped-def]
        assert client.get("/api/v1/auth/me").status_code == 200

        csrf_response = client.get("/api/v1/auth/csrf")
        csrf_token = csrf_response.json()["csrf_token"]
        logout_response = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})
        assert logout_response.status_code == 200

        assert client.get("/api/v1/auth/me").status_code == 401

    def test_logout_idempotent(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        # Logout without being logged in should not error (no session = no CSRF needed).
        response = client.post("/api/v1/auth/logout")
        assert response.status_code == 200

    def test_logout_clears_cookie(self, client, clean_redis, registered_user) -> None:  # type: ignore[no-untyped-def]
        csrf_response = client.get("/api/v1/auth/csrf")
        csrf_token = csrf_response.json()["csrf_token"]
        logout_response = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})
        assert logout_response.status_code == 200
        raw_cookie = logout_response.headers.get("set-cookie", "")
        assert "max-age=0" in raw_cookie.lower() or '""' in raw_cookie


class TestCurrentUser:
    """/me endpoint tests."""

    def test_me_authenticated(self, client, clean_redis, registered_user) -> None:  # type: ignore[no-untyped-def]
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 200
        data = response.json()
        assert data["email"].endswith("@example.com")
        assert "password_hash" not in str(data)
        assert "workspaces" in data

    def test_me_unauthenticated(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401

    def test_me_does_not_expose_session_token(self, client, clean_redis, registered_user) -> None:  # type: ignore[no-untyped-def]
        response = client.get("/api/v1/auth/me")
        data = response.json()
        assert "session" not in str(data).lower()
        assert "csrf" not in str(data).lower()
        assert "token" not in str(data).lower()
