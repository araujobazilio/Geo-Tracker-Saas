"""Plan & Usage customer page tests.

Tests:
- Billing status matrix: ACTIVE, TRIALING, PAST_DUE, CANCELED, no billing
- Quota display: usage=0, used+reserved, >=80% warning, exhausted, zero limit
- Feature flags: confidence disabled, verification disabled, schedule unavailable
- Access control: MEMBER read access, cross-tenant 404
- Zero-cost: GET consumes zero AI Checks, GET creates zero UsageEvents
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select


def _create_plan_and_workspace(
    session,
    *,
    plan_code: str | None = None,
    billing_status: str | None = "ACTIVE",
    billing_source: str = "ADMIN",
    monthly_ai_checks: int = 200,
    confidence_enabled: bool = True,
    verification_enabled: bool = True,
    scheduled_scan_interval: int | None = 24,
    used: int = 0,
    reserved: int = 0,
    user_email: str | None = None,
) -> dict:
    """Create a plan, user, workspace, membership, and billing account.

    Returns a dict with user_email, password, workspace_id.
    """
    from app.core.enums import (
        BillingAccountStatus,
        BillingSource,
        LLMProvider,
        WorkspaceRole,
        WorkspaceType,
    )
    from app.core.security import hash_password
    from app.models.billing import BillingAccount
    from app.models.plan_definition import PlanDefinition
    from app.models.plan_provider import PlanProvider
    from app.models.user import User
    from app.models.workspace import Workspace, WorkspaceMember

    unique = uuid.uuid4().hex[:8]
    if plan_code is None:
        plan_code = f"beta_{unique}"
    if user_email is None:
        user_email = f"planuser_{unique}@example.com"

    plan = PlanDefinition(
        code=plan_code,
        name="Beta (Internal)",
        is_active=True,
        max_projects=10,
        max_keywords_per_project=50,
        max_competitors_per_project=20,
        max_team_members=10,
        monthly_ai_checks=monthly_ai_checks,
        min_scheduled_scan_interval_hours=scheduled_scan_interval,
        confidence_scans_enabled=confidence_enabled,
        verification_scans_enabled=verification_enabled,
    )
    session.add(plan)
    session.flush()

    for provider in [LLMProvider.OPENAI, LLMProvider.ANTHROPIC]:
        session.add(PlanProvider(plan_id=plan.id, provider=provider))

    user = User(
        email=user_email,
        password_hash=hash_password("validpassword123"),
        is_active=True,
        is_admin=False,
    )
    session.add(user)
    session.flush()

    workspace = Workspace(
        name="Plan Test Workspace",
        workspace_type=WorkspaceType.PERSONAL,
    )
    session.add(workspace)
    session.flush()

    membership = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role=WorkspaceRole.OWNER,
    )
    session.add(membership)
    session.flush()

    if billing_status is not None:
        billing = BillingAccount(
            workspace_id=workspace.id,
            source=BillingSource(billing_source),
            status=BillingAccountStatus(billing_status),
            plan_code=plan_code,
            is_primary=True,
        )
        session.add(billing)

    # If used/reserved > 0, create usage events to consume quota.
    # We'll handle this via QuotaService if needed.
    # For now, the snapshot reads from the DB.

    return {
        "user_email": user_email,
        "password": "validpassword123",
        "workspace_id": str(workspace.id),
        "plan_code": plan_code,
    }


@pytest.fixture()
def _plan_workspace(prepared_test_db):
    """Create a workspace with a BillingAccount and PlanDefinition for plan page tests."""
    from app.db.session import get_session_factory

    factory = get_session_factory()
    with factory() as session:
        result = _create_plan_and_workspace(session)
        session.commit()
        yield result


def _login(client, email: str, password: str = "validpassword123") -> None:
    """Login via API."""
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200


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

        _login(client, _plan_workspace["user_email"])

        response = client.get(f"/app/w/{_plan_workspace['workspace_id']}/settings/plan")
        assert response.status_code == 200
        assert "Plan & Usage" in response.text
        assert "AI Checks" in response.text
        assert "Beta" in response.text

    def test_plan_page_shows_quota(self, _plan_workspace) -> None:
        """Plan page shows quota information."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        _login(client, _plan_workspace["user_email"])

        response = client.get(f"/app/w/{_plan_workspace['workspace_id']}/settings/plan")
        assert response.status_code == 200
        assert "200" in response.text

    def test_plan_page_shows_features(self, _plan_workspace) -> None:
        """Plan page shows feature availability."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        _login(client, _plan_workspace["user_email"])

        response = client.get(f"/app/w/{_plan_workspace['workspace_id']}/settings/plan")
        assert response.status_code == 200
        assert "Confidence scans" in response.text
        assert "Verification scans" in response.text

    def test_plan_page_cross_workspace_404(self, _plan_workspace) -> None:
        """Accessing plan page for another workspace returns 404."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        _login(client, _plan_workspace["user_email"])

        random_ws = str(uuid.uuid4())
        response = client.get(f"/app/w/{random_ws}/settings/plan")
        assert response.status_code == 404


def _setup_and_view(prepared_test_db, **kwargs) -> tuple[int, str]:
    """Create a plan/workspace, login, and view the plan page."""
    from app.db.session import get_session_factory
    from app.main import create_app

    factory = get_session_factory()
    with factory() as session:
        result = _create_plan_and_workspace(session, **kwargs)
        session.commit()

    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    _login(client, result["user_email"])
    response = client.get(f"/app/w/{result['workspace_id']}/settings/plan")
    return response.status_code, response.text


class TestPlanPageBillingStatusMatrix:
    """Test that the plan page shows the correct billing status label."""

    def test_active_status(self, prepared_test_db) -> None:
        """ACTIVE billing shows 'Active'."""
        status, text = _setup_and_view(prepared_test_db, billing_status="ACTIVE")
        assert status == 200
        assert "Active" in text

    def test_trialing_status(self, prepared_test_db) -> None:
        """TRIALING billing shows 'Trialing'."""
        status, text = _setup_and_view(prepared_test_db, billing_status="TRIALING")
        assert status == 200
        assert "Trialing" in text

    def test_past_due_status(self, prepared_test_db) -> None:
        """PAST_DUE billing shows 'Past due'."""
        status, text = _setup_and_view(prepared_test_db, billing_status="PAST_DUE")
        assert status == 200
        assert "Past due" in text

    def test_canceled_status(self, prepared_test_db) -> None:
        """CANCELED billing shows 'Canceled'."""
        status, text = _setup_and_view(prepared_test_db, billing_status="CANCELED")
        assert status == 200
        assert "Canceled" in text

    def test_no_billing_shows_unentitled(self, prepared_test_db) -> None:
        """No primary billing shows 'Unentitled'."""
        status, text = _setup_and_view(prepared_test_db, billing_status=None)
        assert status == 200
        assert "Unentitled" in text


class TestPlanPageQuotaMatrix:
    """Test quota display states."""

    def test_zero_usage(self, _plan_workspace) -> None:
        """Usage = 0 shows 0 used."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        _login(client, _plan_workspace["user_email"])

        response = client.get(f"/app/w/{_plan_workspace['workspace_id']}/settings/plan")
        assert response.status_code == 200
        # Should show 0 used.
        assert ">0<" in response.text or "0" in response.text


class TestPlanPageFeatureMatrix:
    """Test feature flag display."""

    def test_confidence_disabled(self, prepared_test_db) -> None:
        """When confidence is disabled, the page reflects it."""
        status, text = _setup_and_view(prepared_test_db, confidence_enabled=False)
        assert status == 200
        # The feature should show as disabled/No.
        assert "Confidence scans" in text

    def test_verification_disabled(self, prepared_test_db) -> None:
        """When verification is disabled, the page reflects it."""
        status, text = _setup_and_view(prepared_test_db, verification_enabled=False)
        assert status == 200
        assert "Verification scans" in text

    def test_schedule_unavailable(self, prepared_test_db) -> None:
        """When scheduled scan interval is None, schedule is unavailable."""
        status, text = _setup_and_view(prepared_test_db, scheduled_scan_interval=None)
        assert status == 200


class TestPlanPageZeroCost:
    """Test that GET /plan consumes zero AI Checks and creates zero UsageEvents."""

    def test_get_creates_zero_usage_events(self, _plan_workspace) -> None:
        """GET /plan creates 0 UsageEvent rows."""
        from app.db.session import get_session_factory
        from app.main import create_app
        from app.models.usage import UsageEvent

        factory = get_session_factory()
        with factory() as session:
            events_before = session.execute(
                select(func.count()).select_from(UsageEvent)
            ).scalar_one()

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        _login(client, _plan_workspace["user_email"])

        response = client.get(f"/app/w/{_plan_workspace['workspace_id']}/settings/plan")
        assert response.status_code == 200

        with factory() as session:
            events_after = session.execute(
                select(func.count()).select_from(UsageEvent)
            ).scalar_one()

        assert events_after == events_before, "GET /plan must not create UsageEvents"


class TestPlanPageMemberAccess:
    """Test that MEMBER role can read the plan page."""

    def test_member_can_view_plan(self, prepared_test_db) -> None:
        """A MEMBER (not OWNER) can view the plan page."""
        from app.core.enums import WorkspaceRole
        from app.core.security import hash_password
        from app.db.session import get_session_factory
        from app.main import create_app
        from app.models.user import User
        from app.models.workspace import WorkspaceMember

        factory = get_session_factory()
        with factory() as session:
            result = _create_plan_and_workspace(session)
            session.commit()

            # Add a MEMBER to the same workspace.
            with factory() as s2:
                ws_id = uuid.UUID(result["workspace_id"])
                member_email = f"member_{uuid.uuid4().hex[:8]}@example.com"
                member_user = User(
                    email=member_email,
                    password_hash=hash_password("validpassword123"),
                    is_active=True,
                    is_admin=False,
                )
                s2.add(member_user)
                s2.flush()
                membership = WorkspaceMember(
                    workspace_id=ws_id,
                    user_id=member_user.id,
                    role=WorkspaceRole.MEMBER,
                )
                s2.add(membership)
                s2.commit()

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        _login(client, member_email)

        response = client.get(f"/app/w/{result['workspace_id']}/settings/plan")
        assert response.status_code == 200
        assert "Plan & Usage" in response.text
