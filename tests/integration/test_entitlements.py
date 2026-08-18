"""Integration tests for EntitlementService.

Tests:
  - no BillingAccount → UNENTITLED
  - inactive billing → UNENTITLED
  - unknown plan → UNENTITLED
  - inactive plan → UNENTITLED
  - active primary billing + active plan → correct entitlements
  - provider allowed / denied
  - feature allowed / denied
  - max project / keyword / competitor / team-member enforcement
  - multiple billing history rows do not cause ambiguous resolution
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    LLMProvider,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import EntitlementDeniedError, QuotaExceededError
from app.models import (
    BillingAccount,
    PlanDefinition,
    PlanProvider,
    Project,
    User,
    Workspace,
)
from app.models.workspace import WorkspaceMember
from app.services.entitlement_service import EntitlementService


def _make_workspace(db: Session, name: str = "Ent WS") -> Workspace:
    user = User(email=f"{name.lower().replace(' ', '.')}@example.com", password_hash="h")
    ws = Workspace(name=name, workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, ws])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db.flush()
    return ws


def _make_plan(
    db: Session,
    code: str = "TEST_PLAN",
    active: bool = True,
    max_projects: int = 3,
    max_keywords: int = 20,
    max_competitors: int = 10,
    max_team_members: int = 5,
    monthly_ai_checks: int = 100,
    confidence: bool = True,
    white_label: bool = False,
    providers: list[LLMProvider] | None = None,
) -> PlanDefinition:
    plan = PlanDefinition(
        code=code,
        name=f"Test Plan {code}",
        is_active=active,
        max_projects=max_projects,
        max_keywords_per_project=max_keywords,
        max_competitors_per_project=max_competitors,
        max_team_members=max_team_members,
        monthly_ai_checks=monthly_ai_checks,
        confidence_scans_enabled=confidence,
        white_label_reports=white_label,
        min_scheduled_scan_interval_hours=24,
    )
    db.add(plan)
    db.flush()
    if providers is not None:
        for p in providers:
            db.add(PlanProvider(plan_id=plan.id, provider=p))
        db.flush()
    return plan


def _make_billing(
    db: Session,
    workspace_id: uuid.UUID,
    plan_code: str | None = "TEST_PLAN",
    status: BillingAccountStatus = BillingAccountStatus.ACTIVE,
    is_primary: bool = True,
    source: BillingSource = BillingSource.ADMIN,
) -> BillingAccount:
    ba = BillingAccount(
        workspace_id=workspace_id,
        source=source,
        status=status,
        plan_code=plan_code,
        is_primary=is_primary,
    )
    db.add(ba)
    db.flush()
    return ba


@pytest.mark.integration
class TestEntitlementResolution:
    def test_no_billing_account_returns_unentitled(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        svc = EntitlementService(db_session)
        ent = svc.get_effective_entitlements(ws.id)
        assert ent.is_unentitled
        assert ent.monthly_ai_checks == 0
        assert ent.allowed_providers == frozenset()
        assert ent.max_projects == 0

    def test_inactive_billing_returns_unentitled(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session)
        _make_billing(db_session, ws.id, status=BillingAccountStatus.CANCELED)
        svc = EntitlementService(db_session)
        ent = svc.get_effective_entitlements(ws.id)
        assert ent.is_unentitled

    def test_past_due_billing_returns_unentitled(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session)
        _make_billing(db_session, ws.id, status=BillingAccountStatus.PAST_DUE)
        svc = EntitlementService(db_session)
        ent = svc.get_effective_entitlements(ws.id)
        assert ent.is_unentitled

    def test_unknown_plan_returns_unentitled(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_billing(db_session, ws.id, plan_code="NONEXISTENT")
        svc = EntitlementService(db_session)
        ent = svc.get_effective_entitlements(ws.id)
        assert ent.is_unentitled

    def test_inactive_plan_returns_unentitled(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session, code="INACTIVE", active=False)
        _make_billing(db_session, ws.id, plan_code="INACTIVE")
        svc = EntitlementService(db_session)
        ent = svc.get_effective_entitlements(ws.id)
        assert ent.is_unentitled

    def test_no_plan_code_returns_unentitled(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_billing(db_session, ws.id, plan_code=None)
        svc = EntitlementService(db_session)
        ent = svc.get_effective_entitlements(ws.id)
        assert ent.is_unentitled

    def test_active_plan_returns_correct_entitlements(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(
            db_session,
            code="ACTIVE_PLAN",
            max_projects=5,
            monthly_ai_checks=200,
            providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        )
        _make_billing(db_session, ws.id, plan_code="ACTIVE_PLAN")
        svc = EntitlementService(db_session)
        ent = svc.get_effective_entitlements(ws.id)
        assert not ent.is_unentitled
        assert ent.plan_code == "ACTIVE_PLAN"
        assert ent.max_projects == 5
        assert ent.monthly_ai_checks == 200
        assert LLMProvider.OPENAI in ent.allowed_providers
        assert LLMProvider.ANTHROPIC in ent.allowed_providers
        assert LLMProvider.GOOGLE not in ent.allowed_providers

    def test_trailing_status_returns_entitlements(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session, code="TRIAL_PLAN")
        _make_billing(
            db_session, ws.id, plan_code="TRIAL_PLAN", status=BillingAccountStatus.TRIALING
        )
        svc = EntitlementService(db_session)
        ent = svc.get_effective_entitlements(ws.id)
        assert not ent.is_unentitled
        assert ent.plan_code == "TRIAL_PLAN"

    def test_empty_provider_set_means_no_providers(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session, code="NO_PROVIDERS", providers=[])
        _make_billing(db_session, ws.id, plan_code="NO_PROVIDERS")
        svc = EntitlementService(db_session)
        ent = svc.get_effective_entitlements(ws.id)
        assert ent.allowed_providers == frozenset()

    def test_multiple_billing_history_uses_primary(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session, code="PLAN_A")
        _make_plan(db_session, code="PLAN_B")
        # Old canceled account (not primary).
        _make_billing(
            db_session,
            ws.id,
            plan_code="PLAN_A",
            status=BillingAccountStatus.CANCELED,
            is_primary=False,
        )
        # Current active primary.
        _make_billing(db_session, ws.id, plan_code="PLAN_B", is_primary=True)
        svc = EntitlementService(db_session)
        ent = svc.get_effective_entitlements(ws.id)
        assert ent.plan_code == "PLAN_B"


@pytest.mark.integration
class TestEntitlementEnforcement:
    def test_provider_allowed(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(
            db_session,
            code="PROV_PLAN",
            providers=[LLMProvider.OPENAI],
        )
        _make_billing(db_session, ws.id, plan_code="PROV_PLAN")
        svc = EntitlementService(db_session)
        assert svc.is_provider_allowed(ws.id, LLMProvider.OPENAI)
        assert not svc.is_provider_allowed(ws.id, LLMProvider.GOOGLE)

    def test_require_provider_denied(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session, code="PROV_DENY", providers=[LLMProvider.OPENAI])
        _make_billing(db_session, ws.id, plan_code="PROV_DENY")
        svc = EntitlementService(db_session)
        with pytest.raises(EntitlementDeniedError):
            svc.require_provider(ws.id, LLMProvider.GOOGLE)

    def test_require_feature_denied(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session, code="FEAT_DENY", white_label=False)
        _make_billing(db_session, ws.id, plan_code="FEAT_DENY")
        svc = EntitlementService(db_session)
        with pytest.raises(EntitlementDeniedError):
            svc.require_feature(ws.id, "white_label_reports")

    def test_require_feature_allowed(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session, code="FEAT_OK", white_label=True)
        _make_billing(db_session, ws.id, plan_code="FEAT_OK")
        svc = EntitlementService(db_session)
        svc.require_feature(ws.id, "white_label_reports")  # should not raise

    def test_project_capacity_exceeded(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session, code="CAP_PLAN", max_projects=2)
        _make_billing(db_session, ws.id, plan_code="CAP_PLAN")
        svc = EntitlementService(db_session)
        svc.require_project_capacity(ws.id, 1)  # OK
        with pytest.raises(QuotaExceededError):
            svc.require_project_capacity(ws.id, 2)

    def test_keyword_capacity_exceeded(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session, code="KW_PLAN", max_keywords=5)
        _make_billing(db_session, ws.id, plan_code="KW_PLAN")
        proj = Project(workspace_id=ws.id, name="P", domain="x.com", brand_name="B")
        db_session.add(proj)
        db_session.flush()
        svc = EntitlementService(db_session)
        svc.require_keyword_capacity(ws.id, proj.id, 4)  # OK
        with pytest.raises(QuotaExceededError):
            svc.require_keyword_capacity(ws.id, proj.id, 5)

    def test_competitor_capacity_exceeded(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session, code="COMP_PLAN", max_competitors=3)
        _make_billing(db_session, ws.id, plan_code="COMP_PLAN")
        proj = Project(workspace_id=ws.id, name="P", domain="x.com", brand_name="B")
        db_session.add(proj)
        db_session.flush()
        svc = EntitlementService(db_session)
        svc.require_competitor_capacity(ws.id, proj.id, 2)  # OK
        with pytest.raises(QuotaExceededError):
            svc.require_competitor_capacity(ws.id, proj.id, 3)

    def test_team_member_capacity_exceeded(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _make_plan(db_session, code="TEAM_PLAN", max_team_members=1)
        _make_billing(db_session, ws.id, plan_code="TEAM_PLAN")
        svc = EntitlementService(db_session)
        # Owner already exists (count=1), so adding another should fail.
        with pytest.raises(QuotaExceededError):
            svc.require_team_member_capacity(ws.id)
