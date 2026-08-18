"""Integration tests for QuotaService.

Tests:
  - reserve AI checks
  - commit AI checks
  - release reservation
  - expire stale reservations
  - idempotent reservations (same key returns same record)
  - conflicting idempotency key rejected
  - idempotent usage accounting (same usage key returns same event)
  - release is idempotent
  - expire is idempotent
  - month rollover creates new period
  - quota exceeded when limit reached
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    LLMProvider,
    QuotaReservationStatus,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import ConflictError, QuotaExceededError
from app.models import (
    BillingAccount,
    PlanDefinition,
    PlanProvider,
    User,
    Workspace,
)
from app.models.workspace import WorkspaceMember
from app.services.quota_service import QuotaService, month_period


def _make_workspace(db: Session, name: str = "Quota WS") -> Workspace:
    user = User(email=f"{name.lower().replace(' ', '.')}@example.com", password_hash="h")
    ws = Workspace(name=name, workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, ws])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db.flush()
    return ws


def _setup_plan_and_billing(
    db: Session,
    ws_id: uuid.UUID,
    monthly_ai_checks: int = 100,
    providers: list[LLMProvider] | None = None,
) -> None:
    if providers is None:
        providers = [LLMProvider.OPENAI]
    plan = PlanDefinition(
        code=f"QUOTA_{ws_id.hex[:8]}",
        name="Quota Test Plan",
        is_active=True,
        max_projects=5,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
        max_team_members=5,
        monthly_ai_checks=monthly_ai_checks,
    )
    db.add(plan)
    db.flush()
    for p in providers:
        db.add(PlanProvider(plan_id=plan.id, provider=p))
    db.flush()
    db.add(
        BillingAccount(
            workspace_id=ws_id,
            source=BillingSource.ADMIN,
            status=BillingAccountStatus.ACTIVE,
            plan_code=plan.code,
            is_primary=True,
        )
    )
    db.flush()


@pytest.mark.integration
class TestQuotaReservation:
    def test_reserve_creates_reservation(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, requested_checks=10, idempotency_key="res-1")
        assert res.ai_checks_reserved == 10
        assert res.ai_checks_committed == 0
        assert res.status == QuotaReservationStatus.ACTIVE
        assert res.expires_at is not None

    def test_reserve_increments_reserved_count(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        svc = QuotaService(db_session)
        svc.reserve_ai_checks(ws.id, 10, "res-1")
        snapshot = svc.get_usage_snapshot(ws.id)
        assert snapshot.reserved == 10
        assert snapshot.used == 0
        assert snapshot.remaining == 90

    def test_reserve_exceeds_limit(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=50)
        svc = QuotaService(db_session)
        svc.reserve_ai_checks(ws.id, 30, "res-1")
        with pytest.raises(QuotaExceededError):
            svc.reserve_ai_checks(ws.id, 30, "res-2")

    def test_reserve_idempotent_same_key(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        svc = QuotaService(db_session)
        res1 = svc.reserve_ai_checks(ws.id, 10, "res-1")
        res2 = svc.reserve_ai_checks(ws.id, 10, "res-1")
        assert res1.id == res2.id
        snapshot = svc.get_usage_snapshot(ws.id)
        assert snapshot.reserved == 10  # not doubled

    def test_reserve_conflicting_idempotency_key(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        svc = QuotaService(db_session)
        svc.reserve_ai_checks(ws.id, 10, "res-1")
        with pytest.raises(ConflictError):
            svc.reserve_ai_checks(ws.id, 20, "res-1")  # different amount


@pytest.mark.integration
class TestQuotaCommit:
    def test_commit_transfers_reserved_to_used(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, "res-1")
        event = svc.commit_ai_checks(
            res.id,
            quantity=10,
            usage_idempotency_key="use-1",
            provider="OPENAI",
            model="gpt-4o",
            cost_usd=Decimal("0.01"),
        )
        assert event.ai_checks == 10
        assert event.provider == "OPENAI"
        assert event.cost_usd == Decimal("0.01")
        snapshot = svc.get_usage_snapshot(ws.id)
        assert snapshot.used == 10
        assert snapshot.reserved == 0

    def test_commit_marks_reservation_committed(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, "res-1")
        svc.commit_ai_checks(res.id, 10, "use-1")
        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.COMMITTED
        assert res.ai_checks_committed == 10

    def test_commit_partial(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 80, "res-1")
        svc.commit_ai_checks(res.id, 50, "use-1")
        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.ACTIVE
        assert res.ai_checks_committed == 50
        snapshot = svc.get_usage_snapshot(ws.id)
        assert snapshot.used == 50
        assert snapshot.reserved == 30

    def test_commit_idempotent_same_key(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, "res-1")
        event1 = svc.commit_ai_checks(res.id, 10, "use-1")
        event2 = svc.commit_ai_checks(res.id, 10, "use-1")
        assert event1.id == event2.id
        snapshot = svc.get_usage_snapshot(ws.id)
        assert snapshot.used == 10  # not doubled

    def test_commit_exceeds_uncommitted(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, "res-1")
        with pytest.raises(ConflictError):
            svc.commit_ai_checks(res.id, 20, "use-1")


@pytest.mark.integration
class TestQuotaRelease:
    def test_release_returns_unused_reserved(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 80, "res-1")
        svc.commit_ai_checks(res.id, 50, "use-1")
        svc.release_reservation(res.id)
        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.RELEASED
        snapshot = svc.get_usage_snapshot(ws.id)
        assert snapshot.used == 50
        assert snapshot.reserved == 0

    def test_release_is_idempotent(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 80, "res-1")
        svc.commit_ai_checks(res.id, 50, "use-1")
        svc.release_reservation(res.id)
        svc.release_reservation(res.id)  # should not double-subtract
        snapshot = svc.get_usage_snapshot(ws.id)
        assert snapshot.used == 50
        assert snapshot.reserved == 0


@pytest.mark.integration
class TestQuotaExpiration:
    def test_expire_stale_releases_remaining(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        # Use a clock in the past for reservation, then expire with current time.
        past = datetime.now(UTC) - timedelta(hours=2)
        svc = QuotaService(db_session, clock=past)
        res = svc.reserve_ai_checks(ws.id, 80, "res-1")
        svc.commit_ai_checks(res.id, 20, "use-1")

        # Now expire with current time (reservation TTL has passed).
        svc_now = QuotaService(db_session)
        count = svc_now.expire_stale_reservations()
        assert count == 1
        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.EXPIRED
        snapshot = svc_now.get_usage_snapshot(ws.id)
        assert snapshot.used == 20
        assert snapshot.reserved == 0

    def test_expire_is_idempotent(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        past = datetime.now(UTC) - timedelta(hours=2)
        svc = QuotaService(db_session, clock=past)
        res = svc.reserve_ai_checks(ws.id, 80, "res-1")
        svc.commit_ai_checks(res.id, 20, "use-1")

        svc_now = QuotaService(db_session)
        svc_now.expire_stale_reservations()
        count2 = svc_now.expire_stale_reservations()
        assert count2 == 0  # already expired


@pytest.mark.integration
class TestMonthRollover:
    def test_august_usage_does_not_consume_september(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=100)
        aug = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
        svc_aug = QuotaService(db_session, clock=aug)
        res = svc_aug.reserve_ai_checks(ws.id, 50, "res-aug")
        svc_aug.commit_ai_checks(res.id, 50, "use-aug")
        aug_snapshot = svc_aug.get_usage_snapshot(ws.id)
        assert aug_snapshot.used == 50
        assert aug_snapshot.period_start.month == 8

        # September — new period, August usage doesn't carry over.
        sep = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
        svc_sep = QuotaService(db_session, clock=sep)
        sep_snapshot = svc_sep.get_usage_snapshot(ws.id)
        assert sep_snapshot.used == 0
        assert sep_snapshot.reserved == 0
        assert sep_snapshot.period_start.month == 9
        assert sep_snapshot.remaining == 100

    def test_month_period_helper(self) -> None:  # type: ignore[no-untyped-def]
        start, end = month_period(datetime(2026, 8, 15, tzinfo=UTC))
        assert start == datetime(2026, 8, 1, tzinfo=UTC)
        assert end == datetime(2026, 9, 1, tzinfo=UTC)

        start, end = month_period(datetime(2026, 12, 31, 23, 59, tzinfo=UTC))
        assert start == datetime(2026, 12, 1, tzinfo=UTC)
        assert end == datetime(2027, 1, 1, tzinfo=UTC)


@pytest.mark.integration
class TestUsageSnapshot:
    def test_snapshot_clamps_remaining_at_zero(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        _setup_plan_and_billing(db_session, ws.id, monthly_ai_checks=10)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, "res-1")
        svc.commit_ai_checks(res.id, 10, "use-1")
        snapshot = svc.get_usage_snapshot(ws.id)
        assert snapshot.remaining == 0
        assert snapshot.usage_percentage == 100

    def test_unentitled_workspace_snapshot(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws = _make_workspace(db_session)
        # No plan, no billing.
        svc = QuotaService(db_session)
        snapshot = svc.get_usage_snapshot(ws.id)
        assert snapshot.limit == 0
        assert snapshot.remaining == 0
