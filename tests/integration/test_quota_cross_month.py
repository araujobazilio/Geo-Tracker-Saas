"""Cross-month quota integrity tests.

These tests verify that commit/release/expire operations always update
the ORIGINAL usage period (where the reservation was created), not the
current month's period. This is the core Phase 3.1 invariant.

Scenarios:
  - Reserve in August, commit in September → August counters change
  - Reserve in August, release in September → August counters change
  - Reserve in August, partial commit + release in September → August
  - Reserve in August, expire in September → August counters change
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    LLMProvider,
    WorkspaceRole,
    WorkspaceType,
)
from app.models import (
    BillingAccount,
    PlanDefinition,
    PlanProvider,
    User,
    Workspace,
)
from app.models.workspace import WorkspaceMember
from app.services.quota_service import QuotaService

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


def _setup_workspace_with_plan(db: Session, monthly_limit: int = 100) -> Workspace:
    user = User(email=f"xmonth-{uuid.uuid4().hex[:8]}@example.com", password_hash="h")
    ws = Workspace(name="CrossMonth WS", workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, ws])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db.flush()

    plan = PlanDefinition(
        code=f"XM_{uuid.uuid4().hex[:8]}",
        name="CrossMonth Plan",
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
    db.commit()
    return ws


def _get_period_counts(db: Session, workspace_id: uuid.UUID, month: int) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """Return (used, reserved) for the given month in 2026."""
    from sqlalchemy import text

    period_start = datetime(2026, month, 1, tzinfo=UTC)
    result = db.execute(
        text(
            "SELECT ai_checks_used, ai_checks_reserved "
            "FROM workspace_usage_periods "
            "WHERE workspace_id = :ws AND period_start = :ps"
        ),
        {"ws": str(workspace_id), "ps": period_start},
    ).fetchone()
    if result is None:
        return (0, 0)
    return (result[0], result[1])


@pytest.fixture()
def committed_db():  # type: ignore[no-untyped-def]
    """Yield a session factory backed by committed data (not transactional)."""
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.mark.integration
class TestCrossMonthCommit:
    def test_commit_across_month_boundary(self, committed_db) -> None:  # type: ignore[no-untyped-def]
        """Reserve in August, commit in September.

        August: used=80, reserved=0
        September: used=0, reserved=0
        """
        setup_session = committed_db()
        try:
            ws = _setup_workspace_with_plan(setup_session, monthly_limit=100)
            ws_id = ws.id
        finally:
            setup_session.close()

        # Reserve in August.
        aug = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        aug_session = committed_db()
        try:
            aug_svc = QuotaService(aug_session, clock=aug)
            res = aug_svc.reserve_ai_checks(ws_id, 80, f"xmonth-commit-res-{ws_id.hex[:8]}")
            res_id = res.id
        finally:
            aug_session.close()

        # Commit in September.
        sep = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        sep_session = committed_db()
        try:
            sep_svc = QuotaService(sep_session, clock=sep)
            sep_svc.commit_ai_checks(
                res_id, 80, f"xmonth-commit-use-{ws_id.hex[:8]}", provider="OPENAI"
            )
        finally:
            sep_session.close()

        # Verify August counters.
        verify = committed_db()
        try:
            aug_used, aug_reserved = _get_period_counts(verify, ws_id, 8)
            assert aug_used == 80, f"August used should be 80, got {aug_used}"
            assert aug_reserved == 0, f"August reserved should be 0, got {aug_reserved}"

            sep_used, sep_reserved = _get_period_counts(verify, ws_id, 9)
            assert sep_used == 0, f"September used should be 0, got {sep_used}"
            assert sep_reserved == 0, f"September reserved should be 0, got {sep_reserved}"
        finally:
            verify.close()


@pytest.mark.integration
class TestCrossMonthRelease:
    def test_release_across_month_boundary(self, committed_db) -> None:  # type: ignore[no-untyped-def]
        """Reserve in August, release in September.

        August: reserved=0 (released back)
        September: untouched
        """
        setup_session = committed_db()
        try:
            ws = _setup_workspace_with_plan(setup_session, monthly_limit=100)
            ws_id = ws.id
        finally:
            setup_session.close()

        # Reserve in August.
        aug = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        aug_session = committed_db()
        try:
            aug_svc = QuotaService(aug_session, clock=aug)
            res = aug_svc.reserve_ai_checks(ws_id, 80, f"xmonth-rel-res-{ws_id.hex[:8]}")
            res_id = res.id
        finally:
            aug_session.close()

        # Release in September.
        sep = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        sep_session = committed_db()
        try:
            sep_svc = QuotaService(sep_session, clock=sep)
            sep_svc.release_reservation(res_id)
        finally:
            sep_session.close()

        verify = committed_db()
        try:
            aug_used, aug_reserved = _get_period_counts(verify, ws_id, 8)
            assert aug_used == 0
            assert aug_reserved == 0, f"August reserved should be 0, got {aug_reserved}"

            sep_used, sep_reserved = _get_period_counts(verify, ws_id, 9)
            assert sep_used == 0
            assert sep_reserved == 0
        finally:
            verify.close()


@pytest.mark.integration
class TestCrossMonthPartialRelease:
    def test_partial_commit_then_release_across_month(self, committed_db) -> None:  # type: ignore[no-untyped-def]
        """Reserve 80 in August, commit 50 in August, release remaining in September.

        August: used=50, reserved=0
        September: untouched
        """
        setup_session = committed_db()
        try:
            ws = _setup_workspace_with_plan(setup_session, monthly_limit=100)
            ws_id = ws.id
        finally:
            setup_session.close()

        # Reserve + partial commit in August.
        aug = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        aug_session = committed_db()
        try:
            aug_svc = QuotaService(aug_session, clock=aug)
            res = aug_svc.reserve_ai_checks(ws_id, 80, f"xmonth-partial-res-{ws_id.hex[:8]}")
            res_id = res.id
            aug_svc.commit_ai_checks(
                res_id, 50, f"xmonth-partial-use-{ws_id.hex[:8]}", provider="OPENAI"
            )
        finally:
            aug_session.close()

        # Release remaining in September.
        sep = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        sep_session = committed_db()
        try:
            sep_svc = QuotaService(sep_session, clock=sep)
            sep_svc.release_reservation(res_id)
        finally:
            sep_session.close()

        verify = committed_db()
        try:
            aug_used, aug_reserved = _get_period_counts(verify, ws_id, 8)
            assert aug_used == 50
            assert aug_reserved == 0, f"August reserved should be 0, got {aug_reserved}"

            sep_used, sep_reserved = _get_period_counts(verify, ws_id, 9)
            assert sep_used == 0
            assert sep_reserved == 0
        finally:
            verify.close()


@pytest.mark.integration
class TestCrossMonthExpiration:
    def test_expiration_across_month_boundary(self, committed_db) -> None:  # type: ignore[no-untyped-def]
        """Reserve in August with short TTL, expire in September.

        August: reserved=0 (expired back)
        September: untouched, no negative counters
        """
        setup_session = committed_db()
        try:
            ws = _setup_workspace_with_plan(setup_session, monthly_limit=100)
            ws_id = ws.id
        finally:
            setup_session.close()

        # Reserve in August with a clock 2 hours in the past so TTL
        # expires by the time we run cleanup in September.
        aug = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        aug_session = committed_db()
        try:
            # Use a clock far enough in the past that expires_at is
            # well before September 1.
            past = aug - timedelta(hours=2)
            aug_svc = QuotaService(aug_session, clock=past)
            res = aug_svc.reserve_ai_checks(ws_id, 80, f"xmonth-exp-res-{ws_id.hex[:8]}")
            res_id = res.id
        finally:
            aug_session.close()

        # Expire in September.
        sep = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        sep_session = committed_db()
        try:
            sep_svc = QuotaService(sep_session, clock=sep)
            sep_svc.expire_stale_reservations()
        finally:
            sep_session.close()

        verify = committed_db()
        try:
            aug_used, aug_reserved = _get_period_counts(verify, ws_id, 8)
            assert aug_used == 0
            assert aug_reserved == 0, f"August reserved should be 0, got {aug_reserved}"

            sep_used, sep_reserved = _get_period_counts(verify, ws_id, 9)
            assert sep_used == 0
            assert sep_reserved == 0

            # Verify reservation is EXPIRED.
            from sqlalchemy import text

            result = verify.execute(
                text("SELECT status FROM quota_reservations WHERE id = :rid"),
                {"rid": str(res_id)},
            ).fetchone()
            assert result is not None
            assert result[0] == "EXPIRED"
        finally:
            verify.close()
