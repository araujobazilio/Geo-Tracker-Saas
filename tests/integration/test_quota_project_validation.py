"""Cross-workspace project linkage rejection test.

Verifies that reserve_ai_checks() rejects a project_id that does not
belong to the workspace_id being charged. This prevents a malicious or
buggy caller from attributing quota usage to workspace A while linking
it to a project in workspace B.
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
from app.core.exceptions import ConflictError
from app.models import (
    BillingAccount,
    PlanDefinition,
    PlanProvider,
    Project,
    User,
    Workspace,
)
from app.models.workspace import WorkspaceMember
from app.services.quota_service import QuotaService


def _make_workspace_with_plan(db: Session, name: str, monthly_limit: int = 100) -> Workspace:
    user = User(email=f"{name.lower().replace(' ', '.')}@example.com", password_hash="h")
    ws = Workspace(name=name, workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, ws])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db.flush()

    plan = PlanDefinition(
        code=f"PROJ_{uuid.uuid4().hex[:8]}",
        name=f"Plan {name}",
        is_active=True,
        max_projects=5,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
        max_team_members=5,
        monthly_ai_checks=monthly_limit,
    )
    db.add(plan)
    db.flush()
    db.add(PlanProvider(plan_id=plan.id, provider=LLMProvider.OPENAI))
    db.flush()
    db.add(
        BillingAccount(
            workspace_id=ws.id,
            source=BillingSource.ADMIN,
            status=BillingAccountStatus.ACTIVE,
            plan_code=plan.code,
            is_primary=True,
        )
    )
    db.flush()
    return ws


@pytest.mark.integration
class TestCrossWorkspaceProjectRejection:
    def test_project_from_different_workspace_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Reserve with workspace A but project from workspace B → ConflictError."""
        ws_a = _make_workspace_with_plan(db_session, "Workspace A")
        ws_b = _make_workspace_with_plan(db_session, "Workspace B")

        # Create a project in workspace B.
        proj_b = Project(workspace_id=ws_b.id, name="Proj B", domain="b.com", brand_name="BrandB")
        db_session.add(proj_b)
        db_session.flush()

        svc = QuotaService(db_session)
        with pytest.raises(ConflictError):
            svc.reserve_ai_checks(
                ws_a.id,
                requested_checks=10,
                idempotency_key="cross-ws-proj",
                project_id=proj_b.id,
            )

    def test_project_from_same_workspace_accepted(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Reserve with workspace A and project from workspace A → OK."""
        ws_a = _make_workspace_with_plan(db_session, "Workspace A OK")
        proj_a = Project(workspace_id=ws_a.id, name="Proj A", domain="a.com", brand_name="BrandA")
        db_session.add(proj_a)
        db_session.flush()

        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(
            ws_a.id,
            requested_checks=10,
            idempotency_key="same-ws-proj",
            project_id=proj_a.id,
        )
        assert res.project_id == proj_a.id
        assert res.workspace_id == ws_a.id

    def test_nonexistent_project_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Reserve with a nonexistent project_id → ConflictError."""
        ws_a = _make_workspace_with_plan(db_session, "Workspace NE")
        svc = QuotaService(db_session)
        with pytest.raises(ConflictError):
            svc.reserve_ai_checks(
                ws_a.id,
                requested_checks=10,
                idempotency_key="noexist-proj",
                project_id=uuid.uuid4(),
            )

    def test_no_project_id_accepted(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Reserve with no project_id → OK (project is optional)."""
        ws_a = _make_workspace_with_plan(db_session, "Workspace NoProj")
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(
            ws_a.id,
            requested_checks=10,
            idempotency_key="no-proj",
            project_id=None,
        )
        assert res.project_id is None
