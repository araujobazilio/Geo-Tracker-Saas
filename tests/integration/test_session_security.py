"""Integration tests for session security."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.security import generate_session_token
from app.db.redis import get_redis, reset_redis
from app.db.session import reset_engine
from app.main import create_app
from app.models.user import User

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


def _unique_email() -> str:
    return f"sess-{uuid.uuid4().hex[:8]}@example.com"


def _direct_db_session() -> Session:
    """Create a direct DB session that commits are visible to the app."""
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return factory()


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
            json={"email": _unique_email(), "password": "secure-password-123"},
        )
        redis = get_redis()
        keys = redis.keys("geo:session:*")
        assert len(keys) > 0
        for key in keys:
            suffix = key.replace("geo:session:", "")
            assert len(suffix) == 64
            int(suffix, 16)  # valid hex

    def test_new_token_on_login(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        email = _unique_email()
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        cookie_name = get_settings().session_cookie_name
        cookie1 = client.cookies.get(cookie_name)

        client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "secure-password-123"},
        )
        cookie2 = client.cookies.get(cookie_name)

        assert cookie1 != cookie2

    def test_revoked_token_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client.post(
            "/api/v1/auth/register",
            json={"email": _unique_email(), "password": "secure-password-123"},
        )
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})

        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401

    def test_tampered_session_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client.post(
            "/api/v1/auth/register",
            json={"email": _unique_email(), "password": "secure-password-123"},
        )
        cookie_name = get_settings().session_cookie_name
        client.cookies.set(cookie_name, "tampered-token-value")
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401

    def test_unknown_session_rejected(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        cookie_name = get_settings().session_cookie_name
        client.cookies.set(cookie_name, generate_session_token())
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401

    def test_inactive_user_rejected_with_valid_session(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        email = _unique_email()
        client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "secure-password-123"},
        )
        assert client.get("/api/v1/auth/me").status_code == 200

        # Deactivate user via direct DB session (visible to app).
        session = _direct_db_session()
        try:
            session.execute(update(User).where(User.email == email).values(is_active=False))
            session.commit()
        finally:
            session.close()

        response = client.get("/api/v1/auth/me")
        assert response.status_code == 401
