"""Closed beta registration gate tests.

Tests that registration is rejected when REGISTRATION_MODE is closed,
and accepted when open. Covers both API and web routes.

When closed, proves ZERO rows are created (no User, no Workspace,
no WorkspaceMember, no BillingAccount).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select


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


def _count_rows(model, session) -> int:
    """Count rows in a model table."""
    return session.execute(select(func.count()).select_from(model)).scalar_one()


class TestAPIRegistrationGate:
    """Test the API /api/v1/auth/register endpoint."""

    def test_closed_rejects_api_register(self, _closed_registration, prepared_test_db) -> None:
        """API register returns 422 when registration is closed."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/v1/auth/register",
            json={"email": "closedapi@example.com", "password": "validpassword123"},
        )
        assert response.status_code == 422
        assert "closed" in response.json()["error"]["message"].lower()

    def test_closed_api_creates_zero_rows(self, _closed_registration, prepared_test_db) -> None:
        """API register when closed creates 0 User, 0 Workspace, 0 WorkspaceMember, 0 BillingAccount."""
        from app.main import create_app
        from app.models.billing import BillingAccount
        from app.models.user import User
        from app.models.workspace import Workspace, WorkspaceMember

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        from app.db.session import get_session_factory

        factory = get_session_factory()
        with factory() as session:
            users_before = _count_rows(User, session)
            ws_before = _count_rows(Workspace, session)
            members_before = _count_rows(WorkspaceMember, session)
            billing_before = _count_rows(BillingAccount, session)

        email = f"closed_zero_{uuid.uuid4().hex[:8]}@example.com"
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "validpassword123"},
        )
        assert response.status_code == 422

        with factory() as session:
            assert _count_rows(User, session) == users_before
            assert _count_rows(Workspace, session) == ws_before
            assert _count_rows(WorkspaceMember, session) == members_before
            assert _count_rows(BillingAccount, session) == billing_before

    def test_open_accepts_api_register(self, _open_registration, prepared_test_db) -> None:
        """API register succeeds when registration is open."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        email = f"openapi_{uuid.uuid4().hex[:8]}@example.com"
        response = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "validpassword123"},
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
            data={"email": "closedweb@example.com", "password": "validpassword123"},
        )
        assert response.status_code == 403
        assert "closed" in response.text.lower()

    def test_closed_web_creates_zero_rows(self, _closed_registration, prepared_test_db) -> None:
        """Web POST /register when closed creates 0 rows."""
        from app.main import create_app
        from app.models.billing import BillingAccount
        from app.models.user import User
        from app.models.workspace import Workspace, WorkspaceMember

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        from app.db.session import get_session_factory

        factory = get_session_factory()
        with factory() as session:
            users_before = _count_rows(User, session)
            ws_before = _count_rows(Workspace, session)
            members_before = _count_rows(WorkspaceMember, session)
            billing_before = _count_rows(BillingAccount, session)

        email = f"closed_web_zero_{uuid.uuid4().hex[:8]}@example.com"
        response = client.post(
            "/register",
            data={"email": email, "password": "validpassword123"},
        )
        assert response.status_code == 403

        with factory() as session:
            assert _count_rows(User, session) == users_before
            assert _count_rows(Workspace, session) == ws_before
            assert _count_rows(WorkspaceMember, session) == members_before
            assert _count_rows(BillingAccount, session) == billing_before

    def test_open_shows_register_form(self, _open_registration, prepared_test_db) -> None:
        """Web GET /register shows the normal registration form when open."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/register")
        assert response.status_code == 200
        # The normal register form should NOT contain "closed" text.
        assert "registration is currently closed" not in response.text.lower()


class TestLoginStillWorksWhenClosed:
    """Login must still work when registration is closed."""

    def test_login_works_when_closed(self, _closed_registration, prepared_test_db) -> None:
        """Existing users can still log in when registration is closed."""
        from app.core.security import hash_password
        from app.db.session import get_session_factory
        from app.main import create_app
        from app.models.user import User

        # Create a user directly.
        factory = get_session_factory()
        email = f"login_closed_{uuid.uuid4().hex[:8]}@example.com"
        with factory() as session:
            user = User(
                email=email,
                password_hash=hash_password("validpassword123"),
                is_active=True,
                is_admin=False,
            )
            session.add(user)
            session.commit()

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "validpassword123"},
        )
        assert response.status_code == 200
