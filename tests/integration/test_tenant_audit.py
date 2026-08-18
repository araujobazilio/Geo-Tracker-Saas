"""Tests for TENANT_ACCESS_DENIED audit event on cross-tenant access.

Verifies:
  - Cross-tenant access returns HTTP 404 externally.
  - A TENANT_ACCESS_DENIED audit event is created internally.
  - The event references the attempting user and the workspace.
  - No sensitive authentication values are present in the event.
  - Normal authorized access does NOT create a denial event.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.redis import reset_redis
from app.db.session import reset_engine
from app.main import create_app
from app.models.audit import AuditLog

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


def _unique_email() -> str:
    return f"audit-{uuid.uuid4().hex[:8]}@example.com"


def _direct_db_session() -> Session:
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return factory()


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
    from app.db.redis import get_redis

    redis = get_redis()
    redis.flushdb()
    yield
    redis.flushdb()


def _register_and_get_csrf(client, email: str | None = None):  # type: ignore[no-untyped-def]
    if email is None:
        email = _unique_email()
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "secure-password-123"},
    )
    assert response.status_code == 201, response.text
    user_data = response.json()
    csrf_response = client.get("/api/v1/auth/csrf")
    assert csrf_response.status_code == 200
    csrf_token = csrf_response.json()["csrf_token"]
    return client, csrf_token, user_data


def _get_audit_events(action: str, user_id: str | None = None) -> list[AuditLog]:  # type: ignore[no-untyped-def]
    """Query audit events directly from the DB."""
    session = _direct_db_session()
    try:
        stmt = select(AuditLog).where(AuditLog.action == action)
        if user_id is not None:
            stmt = stmt.where(AuditLog.user_id == uuid.UUID(user_id))
        results = list(session.execute(stmt).scalars().all())
        return results
    finally:
        session.close()


class TestTenantAccessDeniedAudit:
    """TENANT_ACCESS_DENIED audit event tests."""

    def test_cross_tenant_access_returns_404_and_cretes_audit_event(
        self, client, clean_redis
    ) -> None:  # type: ignore[no-untyped-def]
        # User A registers and gets a workspace.
        client, _, _ = _register_and_get_csrf(client)
        ws_list_a = client.get("/api/v1/workspaces").json()
        ws_a_id = ws_list_a[0]["id"]

        # User B registers.
        client, _, user_b = _register_and_get_csrf(client)
        user_b_id = user_b["id"]

        # User B tries to read User A's workspace.
        response = client.get(f"/api/v1/workspaces/{ws_a_id}")
        assert response.status_code == 404

        # Verify TENANT_ACCESS_DENIED audit event was created.
        events = _get_audit_events("TENANT_ACCESS_DENIED", user_b_id)
        assert len(events) >= 1
        event = events[0]
        assert event.action == "TENANT_ACCESS_DENIED"
        assert str(event.user_id) == user_b_id
        assert str(event.workspace_id) == ws_a_id
        assert event.entity_type == "workspace"
        assert str(event.entity_id) == ws_a_id

    def test_cross_tenant_update_returns_404_and_creates_audit_event(
        self, client, clean_redis
    ) -> None:  # type: ignore[no-untyped-def]
        client, _, _ = _register_and_get_csrf(client)
        ws_list_a = client.get("/api/v1/workspaces").json()
        ws_a_id = ws_list_a[0]["id"]

        client, csrf_b, user_b = _register_and_get_csrf(client)
        user_b_id = user_b["id"]

        response = client.patch(
            f"/api/v1/workspaces/{ws_a_id}",
            json={"name": "Hacked"},
            headers={"X-CSRF-Token": csrf_b},
        )
        assert response.status_code == 404

        events = _get_audit_events("TENANT_ACCESS_DENIED", user_b_id)
        assert len(events) >= 1

    def test_audit_event_has_no_sensitive_data(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, _, user_a = _register_and_get_csrf(client)
        ws_list_a = client.get("/api/v1/workspaces").json()
        ws_a_id = ws_list_a[0]["id"]

        client, _, user_b = _register_and_get_csrf(client)
        user_b_id = user_b["id"]

        client.get(f"/api/v1/workspaces/{ws_a_id}")

        events = _get_audit_events("TENANT_ACCESS_DENIED", user_b_id)
        assert len(events) >= 1
        event = events[0]

        # Check that no sensitive data is in the metadata.
        metadata_str = str(event.metadata_) if event.metadata_ else ""
        assert "password" not in metadata_str.lower()
        assert "token" not in metadata_str.lower()
        assert "csrf" not in metadata_str.lower()
        assert "session" not in metadata_str.lower()
        assert "cookie" not in metadata_str.lower()
        assert "secret" not in metadata_str.lower()

    def test_authorized_access_does_not_create_denial_event(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        # User A registers and accesses their own workspace.
        client, _, user_a = _register_and_get_csrf(client)
        user_a_id = user_a["id"]
        ws_list = client.get("/api/v1/workspaces").json()
        ws_id = ws_list[0]["id"]

        # Authorized access.
        response = client.get(f"/api/v1/workspaces/{ws_id}")
        assert response.status_code == 200

        # No TENANT_ACCESS_DENIED event should exist for user_a.
        events = _get_audit_events("TENANT_ACCESS_DENIED", user_a_id)
        assert len(events) == 0
