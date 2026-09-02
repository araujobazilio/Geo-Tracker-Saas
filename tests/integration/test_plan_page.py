"""Plan & Usage customer page tests."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _plan_workspace(prepared_test_db):
    """Create a workspace with a BillingAccount and PlanDefinition for plan page tests."""
    from app.core.enums import (
        BillingAccountStatus,
        BillingSource,
        LLMProvider,
        WorkspaceRole,
        WorkspaceType,
    )
    from app.core.security import hash_password
    from app.db.session import get_session_factory
    from app.models.billing import BillingAccount
    from app.models.plan_definition import PlanDefinition
    from app.models.plan_provider import PlanProvider
    from app.models.user import User
    from app.models.workspace import Workspace, WorkspaceMember

    factory = get_session_factory()
    unique = uuid.uuid4().hex[:8]
    plan_code = f"beta_internal_{unique}"
    user_email = f"planuser_{unique}@example.com"
    with factory() as session:
        # Create plan.
        plan = PlanDefinition(
            code=plan_code,
            name="Beta (Internal)",
            is_active=True,
            max_projects=10,
            max_keywords_per_project=50,
            max_competitors_per_project=20,
            max_team_members=10,
            monthly_ai_checks=200,
            min_scheduled_scan_interval_hours=24,
            confidence_scans_enabled=True,
            verification_scans_enabled=True,
        )
        session.add(plan)
        session.flush()

        # Add providers.
        for provider in [LLMProvider.OPENAI, LLMProvider.ANTHROPIC]:
            session.add(PlanProvider(plan_id=plan.id, provider=provider))

        # Create user.
        user = User(
            email=user_email,
            password_hash=hash_password("validpassword123"),
            is_active=True,
            is_admin=False,
        )
        session.add(user)
        session.flush()

        # Create workspace.
        workspace = Workspace(
            name="Plan Test Workspace",
            workspace_type=WorkspaceType.PERSONAL,
        )
        session.add(workspace)
        session.flush()

        # Create membership.
        membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(membership)
        session.flush()

        # Create billing account.
        billing = BillingAccount(
            workspace_id=workspace.id,
            source=BillingSource.ADMIN,
            status=BillingAccountStatus.ACTIVE,
            plan_code=plan_code,
            is_primary=True,
        )
        session.add(billing)
        session.commit()

        yield {
            "user_email": user_email,
            "password": "validpassword123",
            "workspace_id": str(workspace.id),
        }


class TestPlanPage:
    """Test the plan & usage customer page."""

    def test_plan_page_requires_auth(self, _plan_workspace) -> None:
        """Unauthenticated access redirects to /login."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get(
            f"/app/w/{_plan_workspace['workspace_id']}/settings/plan",
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/login" in response.headers.get("location", "")

    def test_plan_page_authenticated(self, _plan_workspace) -> None:
        """Authenticated user can view the plan page."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        # Login.
        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": _plan_workspace["user_email"],
                "password": _plan_workspace["password"],
            },
        )
        assert response.status_code == 200

        # Access plan page.
        response = client.get(f"/app/w/{_plan_workspace['workspace_id']}/settings/plan")
        assert response.status_code == 200
        assert "Plan & Usage" in response.text
        assert "AI Checks" in response.text
        # Should show the plan name.
        assert "Beta" in response.text

    def test_plan_page_shows_quota(self, _plan_workspace) -> None:
        """Plan page shows quota information."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        # Login.
        client.post(
            "/api/v1/auth/login",
            json={
                "email": _plan_workspace["user_email"],
                "password": _plan_workspace["password"],
            },
        )

        response = client.get(f"/app/w/{_plan_workspace['workspace_id']}/settings/plan")
        assert response.status_code == 200
        # Should show the monthly limit (200).
        assert "200" in response.text

    def test_plan_page_shows_features(self, _plan_workspace) -> None:
        """Plan page shows feature availability."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        # Login.
        client.post(
            "/api/v1/auth/login",
            json={
                "email": _plan_workspace["user_email"],
                "password": _plan_workspace["password"],
            },
        )

        response = client.get(f"/app/w/{_plan_workspace['workspace_id']}/settings/plan")
        assert response.status_code == 200
        assert "Confidence scans" in response.text
        assert "Verification scans" in response.text

    def test_plan_page_cross_workspace_404(self, _plan_workspace) -> None:
        """Accessing plan page for another workspace returns 404."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        # Login.
        client.post(
            "/api/v1/auth/login",
            json={
                "email": _plan_workspace["user_email"],
                "password": _plan_workspace["password"],
            },
        )

        # Try a random UUID (not the user's workspace).
        random_ws = str(uuid.uuid4())
        response = client.get(f"/app/w/{random_ws}/settings/plan")
        assert response.status_code == 404
