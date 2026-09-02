"""Closed beta registration gate tests.

Tests that registration is rejected when REGISTRATION_MODE is closed,
and accepted when open. Covers both API and web routes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _open_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set registration mode to open."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("REGISTRATION_MODE", "open")
    get_settings.cache_clear()


@pytest.fixture()
def _closed_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set registration mode to closed."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("REGISTRATION_MODE", "closed")
    get_settings.cache_clear()


class TestAPIRegistrationGate:
    """Test the API /api/v1/auth/register endpoint."""

    def test_closed_rejects_api_register(self, _closed_registration, prepared_test_db) -> None:
        """API register returns 422 when registration is closed."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/v1/auth/register",
            json={"email": "newuser@example.com", "password": "validpassword123"},
        )
        assert response.status_code == 422
        assert "closed" in response.json()["error"]["message"].lower()

    def test_open_accepts_api_register(self, _open_registration, prepared_test_db) -> None:
        """API register succeeds when registration is open."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/v1/auth/register",
            json={"email": "newuser@example.com", "password": "validpassword123"},
        )
        assert response.status_code == 201


class TestWebRegistrationGate:
    """Test the web /register endpoint."""

    def test_closed_shows_closed_page(self, _closed_registration, prepared_test_db) -> None:
        """Web GET /register shows the closed-beta page when registration is closed."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/register")
        assert response.status_code == 200
        assert "closed" in response.text.lower()

    def test_closed_rejects_web_register_post(self, _closed_registration, prepared_test_db) -> None:
        """Web POST /register returns 403 when registration is closed."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/register",
            data={"email": "newuser@example.com", "password": "validpassword123"},
        )
        assert response.status_code == 403
        assert "closed" in response.text.lower()

    def test_open_shows_register_form(self, _open_registration, prepared_test_db) -> None:
        """Web GET /register shows the normal registration form when open."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/register")
        assert response.status_code == 200
        # The normal register form should NOT contain "closed" text.
        assert "registration is currently closed" not in response.text.lower()
