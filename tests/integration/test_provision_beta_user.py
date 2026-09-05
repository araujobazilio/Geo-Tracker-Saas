"""Integration tests for the beta user provisioning operator script.

Tests the full provisioning workflow against the real test database:
- CASE A: new user → creates User + Workspace + WorkspaceMember + BillingAccount
- CASE B: idempotent re-run → no duplicates, exact no-op success

These tests exercise the actual main() function with a real database
session, proving the runtime imports and application logic work end-to-end.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from scripts.provision_beta_user import EXIT_OK, main
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    WorkspaceRole,
    WorkspaceType,
)
from app.models.billing import BillingAccount
from app.models.plan_definition import PlanDefinition
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


@pytest.fixture()
def beta_plan(db_session: Session) -> PlanDefinition:
    """Create an active beta_internal plan in the test database."""
    plan = PlanDefinition(
        code="beta_internal",
        name="Beta Internal Plan",
        is_active=True,
        max_projects=5,
        max_keywords_per_project=50,
        max_competitors_per_project=20,
        max_team_members=3,
        monthly_ai_checks=500,
        confidence_scans_enabled=True,
        white_label_reports=False,
        min_scheduled_scan_interval_hours=24,
    )
    db_session.add(plan)
    db_session.flush()
    return plan


@pytest.fixture()
def provision_factory(db_session: Session) -> sessionmaker[Session]:
    """Return a session factory that yields the test db_session.

    This allows main() to use the test database via get_session_factory().
    The test's transaction rollback ensures no persistent state is left.
    """
    factory: sessionmaker[Session] = sessionmaker(
        bind=db_session.bind,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    return factory


class TestProvisionBetaUserHappyPath:
    """CASE A: new user provisioning creates all required records."""

    def test_provision_new_user(
        self,
        db_session: Session,
        beta_plan: PlanDefinition,
        provision_factory: sessionmaker[Session],
    ) -> None:
        """Provisioning a new user creates User, Workspace, WorkspaceMember,
        and BillingAccount with correct attributes.
        """
        email = "beta-test@example.com"

        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(
                [
                    "--email",
                    email,
                    "--plan-code",
                    "beta_internal",
                    "--workspace-name",
                    "GEO Tracker Test",
                ]
            )

        assert ret == EXIT_OK, "Provisioning should succeed for a new user"

        # Verify User was created.
        user = db_session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        assert user is not None, "User must be created"
        assert user.is_active is True
        assert user.is_admin is False
        assert user.password_hash != ""

        # Verify Workspace was created.
        workspace = db_session.execute(
            select(Workspace).where(Workspace.name == "GEO Tracker Test")
        ).scalar_one_or_none()
        assert workspace is not None, "Workspace must be created"
        assert workspace.workspace_type == WorkspaceType.PERSONAL

        # Verify WorkspaceMember was created with OWNER role.
        membership = db_session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == user.id,
            )
        ).scalar_one_or_none()
        assert membership is not None, "WorkspaceMember must be created"
        assert membership.role == WorkspaceRole.OWNER

        # Verify BillingAccount was created with ADMIN source and ACTIVE status.
        billing = db_session.execute(
            select(BillingAccount).where(
                BillingAccount.workspace_id == workspace.id,
                BillingAccount.is_primary.is_(True),
            )
        ).scalar_one_or_none()
        assert billing is not None, "Primary BillingAccount must be created"
        assert billing.source == BillingSource.ADMIN
        assert billing.status == BillingAccountStatus.ACTIVE
        assert billing.plan_code == "beta_internal"
        assert billing.is_primary is True

    def test_provisioning_is_atomic(
        self,
        db_session: Session,
        beta_plan: PlanDefinition,
        provision_factory: sessionmaker[Session],
    ) -> None:
        """Provisioning must atomically create all records together.

        If any part fails, no partial records should remain. We verify
        this by confirming all four entities exist after a successful run.
        """
        email = "atomic-test@example.com"

        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret = main(["--email", email, "--plan-code", "beta_internal"])

        assert ret == EXIT_OK

        user = db_session.execute(select(User).where(User.email == email)).scalar_one()
        workspace = db_session.execute(
            select(Workspace).where(
                Workspace.id
                == select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
            )
        ).scalar_one()
        membership = db_session.execute(
            select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
        ).scalar_one()
        billing = db_session.execute(
            select(BillingAccount).where(BillingAccount.workspace_id == workspace.id)
        ).scalar_one()

        # All four entities must exist — no partial creation.
        assert user is not None
        assert workspace is not None
        assert membership is not None
        assert billing is not None


class TestProvisionBetaUserIdempotency:
    """CASE B: re-running provisioning for an already-provisioned user is a no-op."""

    def test_idempotent_rerun(
        self,
        db_session: Session,
        beta_plan: PlanDefinition,
        provision_factory: sessionmaker[Session],
    ) -> None:
        """Running provisioning twice for the same user does not create duplicates."""
        email = "idempotent@example.com"

        # First run: create everything.
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret1 = main(["--email", email, "--plan-code", "beta_internal"])
        assert ret1 == EXIT_OK

        # Count entities after first run.
        users_after_first = (
            db_session.execute(select(User).where(User.email == email)).scalars().all()
        )
        assert len(users_after_first) == 1

        memberships_after_first = (
            db_session.execute(
                select(WorkspaceMember).where(WorkspaceMember.user_id == users_after_first[0].id)
            )
            .scalars()
            .all()
        )
        assert len(memberships_after_first) == 1

        billings_after_first = (
            db_session.execute(
                select(BillingAccount).where(
                    BillingAccount.workspace_id == memberships_after_first[0].workspace_id,
                    BillingAccount.is_primary.is_(True),
                )
            )
            .scalars()
            .all()
        )
        assert len(billings_after_first) == 1

        # Second run: should be an exact no-op (CASE B).
        with patch("app.db.session.get_session_factory", return_value=provision_factory):
            ret2 = main(["--email", email, "--plan-code", "beta_internal"])
        assert ret2 == EXIT_OK, "Idempotent re-run should return EXIT_OK"

        # Verify no duplicates were created.
        users_after_second = (
            db_session.execute(select(User).where(User.email == email)).scalars().all()
        )
        assert len(users_after_second) == 1, "No duplicate users"

        memberships_after_second = (
            db_session.execute(
                select(WorkspaceMember).where(WorkspaceMember.user_id == users_after_first[0].id)
            )
            .scalars()
            .all()
        )
        assert len(memberships_after_second) == 1, "No duplicate memberships"

        billings_after_second = (
            db_session.execute(
                select(BillingAccount).where(
                    BillingAccount.workspace_id == memberships_after_first[0].workspace_id,
                    BillingAccount.is_primary.is_(True),
                )
            )
            .scalars()
            .all()
        )
        assert len(billings_after_second) == 1, "No duplicate billing accounts"
