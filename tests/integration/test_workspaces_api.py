"""Integration tests for workspace endpoints and tenant isolation (IDOR)."""

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


def _register_and_get_csrf(client, email: str, password: str = "secure-password-123"):  # type: ignore[no-untyped-def]
    """Register a user and return (client, csrf_token, user_data)."""
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password},
    )
    assert response.status_code == 201, response.text
    user_data = response.json()
    csrf_response = client.get("/api/v1/auth/csrf")
    assert csrf_response.status_code == 200
    csrf_token = csrf_response.json()["csrf_token"]
    return client, csrf_token, user_data


class TestWorkspaceList:
    """GET /api/v1/workspaces tests."""

    def test_list_own_workspaces(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, _, _ = _register_and_get_csrf(client, "list@example.com")
        response = client.get("/api/v1/workspaces")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "My Workspace"

    def test_list_unauthenticated(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        response = client.get("/api/v1/workspaces")
        assert response.status_code == 401


class TestWorkspaceCreate:
    """POST /api/v1/workspaces tests."""

    def test_create_workspace(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, csrf_token, _ = _register_and_get_csrf(client, "create@example.com")
        response = client.post(
            "/api/v1/workspaces",
            json={"name": "Agency WS", "workspace_type": "AGENCY"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "Agency WS"
        assert data["workspace_type"] == "AGENCY"

    def test_creator_becomes_owner(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, csrf_token, _ = _register_and_get_csrf(client, "owner@example.com")
        response = client.post(
            "/api/v1/workspaces",
            json={"name": "New WS", "workspace_type": "PERSONAL"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 201
        ws_id = response.json()["id"]

        # Verify via /me that the user is OWNER.
        me = client.get("/api/v1/auth/me").json()
        ws_roles = {w["id"]: w["role"] for w in me["workspaces"]}
        assert ws_roles[ws_id] == "OWNER"


class TestWorkspaceUpdate:
    """PATCH /api/v1/workspaces/{id} tests."""

    def test_owner_can_update(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, csrf_token, _ = _register_and_get_csrf(client, "update@example.com")
        # Get default workspace.
        ws_list = client.get("/api/v1/workspaces").json()
        ws_id = ws_list[0]["id"]

        response = client.patch(
            f"/api/v1/workspaces/{ws_id}",
            json={"name": "Renamed WS"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed WS"


class TestTenantIsolation:
    """Cross-tenant access (IDOR) tests — MANDATORY."""

    def test_non_member_cannot_read_workspace(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        # User A registers and gets a workspace.
        client, _, _ = _register_and_get_csrf(client, "userA@example.com")
        ws_list_a = client.get("/api/v1/workspaces").json()
        ws_a_id = ws_list_a[0]["id"]

        # User B registers (new session).
        client, _, _ = _register_and_get_csrf(client, "userB@example.com")

        # User B tries to read User A's workspace.
        response = client.get(f"/api/v1/workspaces/{ws_a_id}")
        assert response.status_code == 404  # Not 403 — don't reveal existence.

    def test_non_member_cannot_update_workspace(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        # User A registers and gets a workspace.
        client, _, _ = _register_and_get_csrf(client, "userA@example.com")
        ws_list_a = client.get("/api/v1/workspaces").json()
        ws_a_id = ws_list_a[0]["id"]

        # User B registers.
        client, csrf_b, _ = _register_and_get_csrf(client, "userB@example.com")

        # User B tries to update User A's workspace.
        response = client.patch(
            f"/api/v1/workspaces/{ws_a_id}",
            json={"name": "Hacked WS"},
            headers={"X-CSRF-Token": csrf_b},
        )
        assert response.status_code == 404

    def test_list_only_returns_own_workspaces(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        # User A creates an extra workspace.
        client, csrf_a, _ = _register_and_get_csrf(client, "userA@example.com")
        client.post(
            "/api/v1/workspaces",
            json={"name": "A's Second WS", "workspace_type": "PERSONAL"},
            headers={"X-CSRF-Token": csrf_a},
        )

        # User B registers.
        client, _, _ = _register_and_get_csrf(client, "userB@example.com")

        # User B should only see their own workspace.
        ws_list = client.get("/api/v1/workspaces").json()
        assert len(ws_list) == 1
        assert ws_list[0]["name"] == "My Workspace"
