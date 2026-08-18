"""Database constraint tests for Phase 3 models.

Verifies database rejects:
  - negative plan limits
  - negative used/reserved quota
  - reservation with zero/negative ai_checks_reserved
  - committed > reserved
  - duplicate workspace/month usage period
  - duplicate reservation idempotency key
  - duplicate UsageEvent idempotency key
  - multiple primary billing accounts for same workspace
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    UsageEventType,
    WorkspaceRole,
    WorkspaceType,
)
from app.models import (
    BillingAccount,
    PlanDefinition,
    QuotaReservation,
    UsageEvent,
    User,
    Workspace,
    WorkspaceUsagePeriod,
)
from app.models.workspace import WorkspaceMember


def _make_workspace(db, name: str = "Constraint WS") -> Workspace:  # type: ignore[no-untyped-def]
    user = User(email=f"{name.lower().replace(' ', '.')}@example.com", password_hash="h")
    ws = Workspace(name=name, workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, ws])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db.flush()
    return ws


@pytest.mark.integration
class TestPlanDefinitionConstraints:
    def test_negative_max_projects_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        plan = PlanDefinition(code="NEG1", name="Neg", max_projects=-1)
        db_session.add(plan)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_negative_monthly_ai_checks_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        plan = PlanDefinition(code="NEG2", name="Neg", monthly_ai_checks=-5)
        db_session.add(plan)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_negative_scan_interval_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        plan = PlanDefinition(code="NEG3", name="Neg", min_scheduled_scan_interval_hours=-1)
        db_session.add(plan)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_zero_scan_interval_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        plan = PlanDefinition(code="NEG4", name="Neg", min_scheduled_scan_interval_hours=0)
        db_session.add(plan)
        with pytest.raises(IntegrityError):
            db_session.flush()


@pytest.mark.integration
class TestWorkspaceUsagePeriodConstraints:
    def test_negative_used_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        period = WorkspaceUsagePeriod(
            workspace_id=ws.id,
            period_start=datetime(2026, 8, 1, tzinfo=UTC),
            period_end=datetime(2026, 9, 1, tzinfo=UTC),
            ai_checks_used=-1,
        )
        db_session.add(period)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_negative_reserved_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        period = WorkspaceUsagePeriod(
            workspace_id=ws.id,
            period_start=datetime(2026, 8, 1, tzinfo=UTC),
            period_end=datetime(2026, 9, 1, tzinfo=UTC),
            ai_checks_reserved=-1,
        )
        db_session.add(period)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_duplicate_workspace_period_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        ps = datetime(2026, 8, 1, tzinfo=UTC)
        pe = datetime(2026, 9, 1, tzinfo=UTC)
        db_session.add(WorkspaceUsagePeriod(workspace_id=ws.id, period_start=ps, period_end=pe))
        db_session.flush()
        db_session.add(WorkspaceUsagePeriod(workspace_id=ws.id, period_start=ps, period_end=pe))
        with pytest.raises(IntegrityError):
            db_session.flush()


@pytest.mark.integration
class TestQuotaReservationConstraints:
    def test_zero_reserved_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        res = QuotaReservation(
            workspace_id=ws.id,
            idempotency_key="zero-res",
            ai_checks_reserved=0,
        )
        db_session.add(res)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_negative_reserved_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        res = QuotaReservation(
            workspace_id=ws.id,
            idempotency_key="neg-res",
            ai_checks_reserved=-5,
        )
        db_session.add(res)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_committed_exceeds_reserved_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        res = QuotaReservation(
            workspace_id=ws.id,
            idempotency_key="over-commit",
            ai_checks_reserved=10,
            ai_checks_committed=15,
        )
        db_session.add(res)
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_duplicate_idempotency_key_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        db_session.add(
            QuotaReservation(
                workspace_id=ws.id,
                idempotency_key="dup-key",
                ai_checks_reserved=10,
            )
        )
        db_session.flush()
        db_session.add(
            QuotaReservation(
                workspace_id=ws.id,
                idempotency_key="dup-key",
                ai_checks_reserved=20,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()


@pytest.mark.integration
class TestUsageEventIdempotencyConstraint:
    def test_duplicate_usage_idempotency_key_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        db_session.add(
            UsageEvent(
                workspace_id=ws.id,
                event_type=UsageEventType.AI_CHECK,
                ai_checks=1,
                idempotency_key="dup-use",
            )
        )
        db_session.flush()
        db_session.add(
            UsageEvent(
                workspace_id=ws.id,
                event_type=UsageEventType.AI_CHECK,
                ai_checks=1,
                idempotency_key="dup-use",
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_null_idempotency_key_allowed_multiple(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        db_session.add(
            UsageEvent(
                workspace_id=ws.id,
                event_type=UsageEventType.AI_CHECK,
                ai_checks=1,
                idempotency_key=None,
            )
        )
        db_session.add(
            UsageEvent(
                workspace_id=ws.id,
                event_type=UsageEventType.AI_CHECK,
                ai_checks=1,
                idempotency_key=None,
            )
        )
        db_session.flush()  # should succeed


@pytest.mark.integration
class TestBillingAccountPrimaryConstraint:
    def test_multiple_primary_billing_accounts_rejected(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        db_session.add(
            BillingAccount(
                workspace_id=ws.id,
                source=BillingSource.ADMIN,
                status=BillingAccountStatus.ACTIVE,
                plan_code="P1",
                is_primary=True,
            )
        )
        db_session.flush()
        db_session.add(
            BillingAccount(
                workspace_id=ws.id,
                source=BillingSource.STRIPE,
                status=BillingAccountStatus.ACTIVE,
                plan_code="P2",
                is_primary=True,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_multiple_non_primary_allowed(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        db_session.add(
            BillingAccount(
                workspace_id=ws.id,
                source=BillingSource.ADMIN,
                plan_code="P1",
                is_primary=False,
            )
        )
        db_session.add(
            BillingAccount(
                workspace_id=ws.id,
                source=BillingSource.STRIPE,
                plan_code="P2",
                is_primary=False,
            )
        )
        db_session.flush()  # should succeed
