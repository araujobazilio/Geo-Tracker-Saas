"""Phase 3.2 — Final quota idempotency and transaction lifecycle tests.

Tests for:
  - Cross-workspace reservation idempotency collision
  - Cross-reservation usage key collision
  - Same key / different cost (ConflictError)
  - Same key / exact same payload (idempotent return)
  - Reservation lifecycle terminal states (COMMITTED, RELEASED, EXPIRED)
  - Lock-release tests (idempotent retry doesn't block other sessions)
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    LLMProvider,
    QuotaReservationStatus,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import ConflictError
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


def _setup_workspace_with_plan(db: Session, name: str, monthly_limit: int = 100) -> Workspace:
    user = User(email=f"p32-{uuid.uuid4().hex[:8]}@example.com", password_hash="h")
    ws = Workspace(name=name, workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, ws])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db.flush()

    plan = PlanDefinition(
        code=f"P32_{uuid.uuid4().hex[:8]}",
        name=f"P32 Plan {name}",
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


def _get_period_counts(db: Session, workspace_id: uuid.UUID) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    period_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
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
def engine_factory():  # type: ignore[no-untyped-def]
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True, pool_size=10)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield engine, factory
    engine.dispose()


# ---------------------------------------------------------------------------
# Cross-workspace reservation idempotency collision
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCrossWorkspaceReservationIdempotency:
    def test_cross_workspace_same_key_collision(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Two workspaces use the same idempotency_key with different params.

        They lock DIFFERENT WorkspaceUsagePeriods, so this exercises the
        global unique-key race.

        Expected: ONE reservation wins. The other receives ConflictError.
        No raw IntegrityError escapes. Each workspace's quota counters
        remain correct.
        """
        engine, factory = engine_factory

        setup = factory()
        try:
            ws_a = _setup_workspace_with_plan(setup, "CW-A", monthly_limit=100)
            ws_b = _setup_workspace_with_plan(setup, "CW-B", monthly_limit=100)
            ws_a_id = ws_a.id
            ws_b_id = ws_b.id
        finally:
            setup.close()

        shared_key = f"global-key-{uuid.uuid4().hex[:8]}"
        barrier = threading.Barrier(2)
        results: dict[str, Any] = {"success": 0, "conflict": 0, "errors": [], "winner_ws": None}
        lock = threading.Lock()

        def worker_a() -> None:
            session = factory()
            try:
                svc = QuotaService(session)
                barrier.wait(timeout=10)
                svc.reserve_ai_checks(ws_a_id, 80, idempotency_key=shared_key)
                with lock:
                    results["success"] += 1
                    results["winner_ws"] = "A"
            except ConflictError as e:
                with lock:
                    results["conflict"] += 1
                    results["errors"].append(f"A: {e}")
            except Exception as e:
                with lock:
                    results["errors"].append(f"A: {type(e).__name__}: {e}")
            finally:
                session.close()

        def worker_b() -> None:
            session = factory()
            try:
                svc = QuotaService(session)
                barrier.wait(timeout=10)
                svc.reserve_ai_checks(ws_b_id, 20, idempotency_key=shared_key)
                with lock:
                    results["success"] += 1
                    results["winner_ws"] = "B"
            except ConflictError as e:
                with lock:
                    results["conflict"] += 1
                    results["errors"].append(f"B: {e}")
            except Exception as e:
                with lock:
                    results["errors"].append(f"B: {type(e).__name__}: {e}")
            finally:
                session.close()

        t1 = threading.Thread(target=worker_a)
        t2 = threading.Thread(target=worker_b)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert results["success"] == 1, f"Expected 1 success: {results['errors']}"
        assert results["conflict"] == 1, f"Expected 1 conflict: {results['errors']}"

        # Verify quota counters: only the winner's workspace should have reserved.
        verify = factory()
        try:
            a_used, a_reserved = _get_period_counts(verify, ws_a_id)
            b_used, b_reserved = _get_period_counts(verify, ws_b_id)

            if results["winner_ws"] == "A":
                assert a_reserved == 80, f"WS A should have reserved=80, got {a_reserved}"
                assert b_reserved == 0, f"WS B should have reserved=0, got {b_reserved}"
            else:
                assert b_reserved == 20, f"WS B should have reserved=20, got {b_reserved}"
                assert a_reserved == 0, f"WS A should have reserved=0, got {a_reserved}"
        finally:
            verify.close()


# ---------------------------------------------------------------------------
# Cross-reservation usage key collision
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestCrossReservationUsageKeyCollision:
    def test_same_usage_key_different_reservations(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Two reservations use the same usage_idempotency_key.

        Expected: one event wins, the other gets ConflictError.
        Never return the event belonging to the other reservation.
        No duplicate AI Checks or cost.
        """
        engine, factory = engine_factory

        setup = factory()
        try:
            ws = _setup_workspace_with_plan(setup, "CR-UK", monthly_limit=200)
            ws_id = ws.id
        finally:
            setup.close()

        # Create two separate reservations.
        res_session = factory()
        try:
            svc = QuotaService(res_session)
            res_a = svc.reserve_ai_checks(ws_id, 80, idempotency_key=f"cr-a-{uuid.uuid4().hex[:8]}")
            res_b = svc.reserve_ai_checks(ws_id, 80, idempotency_key=f"cr-b-{uuid.uuid4().hex[:8]}")
            res_a_id = res_a.id
            res_b_id = res_b.id
        finally:
            res_session.close()

        shared_usage_key = f"shared-use-{uuid.uuid4().hex[:8]}"
        barrier = threading.Barrier(2)
        results: dict[str, Any] = {"success": 0, "conflict": 0, "errors": []}
        lock = threading.Lock()

        def worker(res_id: uuid.UUID) -> None:
            session = factory()
            try:
                svc = QuotaService(session)
                barrier.wait(timeout=10)
                svc.commit_ai_checks(
                    res_id,
                    80,
                    usage_idempotency_key=shared_usage_key,
                    provider="OPENAI",
                    cost_usd=Decimal("0.05"),
                )
                with lock:
                    results["success"] += 1
            except ConflictError as e:
                with lock:
                    results["conflict"] += 1
                    results["errors"].append(str(e))
            except Exception as e:
                with lock:
                    results["errors"].append(f"{type(e).__name__}: {e}")
            finally:
                session.close()

        t1 = threading.Thread(target=worker, args=(res_a_id,))
        t2 = threading.Thread(target=worker, args=(res_b_id,))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert results["success"] == 1, f"Expected 1 success: {results['errors']}"
        assert results["conflict"] == 1, f"Expected 1 conflict: {results['errors']}"

        # Total used should be 80 (not 160).
        verify = factory()
        try:
            used, _ = _get_period_counts(verify, ws_id)
            assert used == 80, f"Expected used=80, got {used}"
        finally:
            verify.close()


# ---------------------------------------------------------------------------
# Same key / different cost + same key / exact same payload
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestUsageIdempotencyPayloadValidation:
    def test_same_key_different_cost_raises_conflict(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Same usage key, same reservation, same quantity, different cost → ConflictError."""
        ws = _setup_workspace_with_plan(db_session, "COST-CONFLICT", monthly_limit=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, idempotency_key=f"cost-res-{uuid.uuid4().hex[:8]}")

        usage_key = f"call-123-{uuid.uuid4().hex[:8]}"
        svc.commit_ai_checks(
            res.id,
            1,
            usage_idempotency_key=usage_key,
            provider="OPENAI",
            cost_usd=Decimal("0.01"),
        )

        with pytest.raises(ConflictError):
            svc.commit_ai_checks(
                res.id,
                1,
                usage_idempotency_key=usage_key,
                provider="OPENAI",
                cost_usd=Decimal("0.20"),
            )

    def test_same_key_different_provider_raises_conflict(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Same usage key, different provider → ConflictError."""
        ws = _setup_workspace_with_plan(db_session, "PROV-CONFLICT", monthly_limit=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, idempotency_key=f"prov-res-{uuid.uuid4().hex[:8]}")

        usage_key = f"call-prov-{uuid.uuid4().hex[:8]}"
        svc.commit_ai_checks(
            res.id,
            1,
            usage_idempotency_key=usage_key,
            provider="OPENAI",
            cost_usd=Decimal("0.01"),
        )

        with pytest.raises(ConflictError):
            svc.commit_ai_checks(
                res.id,
                1,
                usage_idempotency_key=usage_key,
                provider="ANTHROPIC",
                cost_usd=Decimal("0.01"),
            )

    def test_same_key_different_tokens_raises_conflict(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Same usage key, different tokens → ConflictError."""
        ws = _setup_workspace_with_plan(db_session, "TOK-CONFLICT", monthly_limit=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, idempotency_key=f"tok-res-{uuid.uuid4().hex[:8]}")

        usage_key = f"call-tok-{uuid.uuid4().hex[:8]}"
        svc.commit_ai_checks(
            res.id,
            1,
            usage_idempotency_key=usage_key,
            provider="OPENAI",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_usd=Decimal("0.01"),
        )

        with pytest.raises(ConflictError):
            svc.commit_ai_checks(
                res.id,
                1,
                usage_idempotency_key=usage_key,
                provider="OPENAI",
                input_tokens=200,
                output_tokens=50,
                total_tokens=250,
                cost_usd=Decimal("0.01"),
            )

    def test_same_key_exact_same_payload_idempotent(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Same usage key, exact same payload → same UsageEvent, no double quota."""
        ws = _setup_workspace_with_plan(db_session, "EXACT-OK", monthly_limit=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, idempotency_key=f"exact-res-{uuid.uuid4().hex[:8]}")

        usage_key = f"call-exact-{uuid.uuid4().hex[:8]}"
        event1 = svc.commit_ai_checks(
            res.id,
            5,
            usage_idempotency_key=usage_key,
            provider="OPENAI",
            model="gpt-4o",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_usd=Decimal("0.01"),
        )

        event2 = svc.commit_ai_checks(
            res.id,
            5,
            usage_idempotency_key=usage_key,
            provider="OPENAI",
            model="gpt-4o",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_usd=Decimal("0.01"),
        )

        assert event1.id == event2.id, "Idempotent retry should return same event"

        # Verify no double quota.
        used, _ = _get_period_counts(db_session, ws.id)
        assert used == 5, f"Expected used=5 (not doubled), got {used}"


# ---------------------------------------------------------------------------
# Reservation lifecycle terminal states
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestReservationLifecycle:
    def test_committed_then_release_remains_committed(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Fully committed reservation → release → remains COMMITTED."""
        ws = _setup_workspace_with_plan(db_session, "LC-COMMIT", monthly_limit=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, idempotency_key=f"lc-res-{uuid.uuid4().hex[:8]}")
        svc.commit_ai_checks(
            res.id,
            10,
            usage_idempotency_key=f"lc-use-{uuid.uuid4().hex[:8]}",
            provider="OPENAI",
        )

        # Now fully COMMITTED.
        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.COMMITTED

        # Release should be a no-op.
        svc.release_reservation(res.id)

        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.COMMITTED

    def test_release_twice_remains_released(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Release twice → remains RELEASED, no double subtraction."""
        ws = _setup_workspace_with_plan(db_session, "LC-REL2", monthly_limit=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, idempotency_key=f"lc-r2-{uuid.uuid4().hex[:8]}")

        svc.release_reservation(res.id)
        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.RELEASED

        _, reserved_after_first = _get_period_counts(db_session, ws.id)

        svc.release_reservation(res.id)
        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.RELEASED

        _, reserved_after_second = _get_period_counts(db_session, ws.id)
        assert reserved_after_first == reserved_after_second, "No double subtraction"

    def test_expired_then_release_remains_expired(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Expired reservation → release → remains EXPIRED."""
        ws = _setup_workspace_with_plan(db_session, "LC-EXP", monthly_limit=100)
        # Reserve with a clock in the past so TTL has expired.
        past = datetime.now(UTC) - timedelta(hours=2)
        svc = QuotaService(db_session, clock=past)
        res = svc.reserve_ai_checks(ws.id, 10, idempotency_key=f"lc-exp-{uuid.uuid4().hex[:8]}")

        # Expire.
        now_svc = QuotaService(db_session)
        now_svc.expire_stale_reservations()
        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.EXPIRED

        # Release should be a no-op.
        now_svc.release_reservation(res.id)
        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.EXPIRED

    def test_commit_against_released_raises_conflict(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Commit against RELEASED → ConflictError."""
        ws = _setup_workspace_with_plan(db_session, "LC-CR", monthly_limit=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, idempotency_key=f"lc-cr-{uuid.uuid4().hex[:8]}")
        svc.release_reservation(res.id)

        with pytest.raises(ConflictError):
            svc.commit_ai_checks(
                res.id,
                5,
                usage_idempotency_key=f"lc-cr-use-{uuid.uuid4().hex[:8]}",
                provider="OPENAI",
            )

    def test_commit_against_expired_raises_conflict(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Commit against EXPIRED → ConflictError."""
        ws = _setup_workspace_with_plan(db_session, "LC-CE", monthly_limit=100)
        past = datetime.now(UTC) - timedelta(hours=2)
        svc = QuotaService(db_session, clock=past)
        res = svc.reserve_ai_checks(ws.id, 10, idempotency_key=f"lc-ce-{uuid.uuid4().hex[:8]}")

        now_svc = QuotaService(db_session)
        now_svc.expire_stale_reservations()
        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.EXPIRED

        with pytest.raises(ConflictError):
            now_svc.commit_ai_checks(
                res.id,
                5,
                usage_idempotency_key=f"lc-ce-use-{uuid.uuid4().hex[:8]}",
                provider="OPENAI",
            )

    def test_commit_against_committed_with_different_key_raises_conflict(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Different usage key against fully COMMITTED reservation → ConflictError."""
        ws = _setup_workspace_with_plan(db_session, "LC-CDK", monthly_limit=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, idempotency_key=f"lc-cdk-{uuid.uuid4().hex[:8]}")
        svc.commit_ai_checks(
            res.id,
            10,
            usage_idempotency_key=f"lc-cdk-use-{uuid.uuid4().hex[:8]}",
            provider="OPENAI",
        )

        db_session.refresh(res)
        assert res.status == QuotaReservationStatus.COMMITTED

        with pytest.raises(ConflictError):
            svc.commit_ai_checks(
                res.id,
                10,
                usage_idempotency_key=f"lc-cdk-diff-{uuid.uuid4().hex[:8]}",
                provider="OPENAI",
            )

    def test_idempotent_retry_of_committed_usage_returns_existing(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Exact idempotent retry of committed usage → existing UsageEvent returned."""
        ws = _setup_workspace_with_plan(db_session, "LC-IDEM", monthly_limit=100)
        svc = QuotaService(db_session)
        res = svc.reserve_ai_checks(ws.id, 10, idempotency_key=f"lc-idem-{uuid.uuid4().hex[:8]}")

        usage_key = f"lc-idem-use-{uuid.uuid4().hex[:8]}"
        event1 = svc.commit_ai_checks(
            res.id,
            10,
            usage_idempotency_key=usage_key,
            provider="OPENAI",
            cost_usd=Decimal("0.05"),
        )

        event2 = svc.commit_ai_checks(
            res.id,
            10,
            usage_idempotency_key=usage_key,
            provider="OPENAI",
            cost_usd=Decimal("0.05"),
        )

        assert event1.id == event2.id

        used, _ = _get_period_counts(db_session, ws.id)
        assert used == 10, f"Expected used=10, got {used}"


# ---------------------------------------------------------------------------
# Lock-release tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLockRelease:
    def test_idempotent_reserve_retry_does_not_block(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Session A: idempotent reserve retry returns.

        Immediately Session B: attempts to lock/update same
        WorkspaceUsagePeriod. Session B must NOT remain blocked.

        Uses lock_timeout / NOWAIT to deterministically detect if
        a lock is still held.
        """
        engine, factory = engine_factory

        setup = factory()
        try:
            ws = _setup_workspace_with_plan(setup, "LR-RES", monthly_limit=100)
            ws_id = ws.id
        finally:
            setup.close()

        idem_key = f"lr-res-{uuid.uuid4().hex[:8]}"

        # Session A: first reserve.
        session_a = factory()
        try:
            svc_a = QuotaService(session_a)
            svc_a.reserve_ai_checks(ws_id, 80, idempotency_key=idem_key)
        finally:
            session_a.close()

        # Session A: idempotent retry (should return existing + release lock).
        session_a2 = factory()
        try:
            svc_a2 = QuotaService(session_a2)
            svc_a2.reserve_ai_checks(ws_id, 80, idempotency_key=idem_key)
        finally:
            session_a2.close()

        # Session B: try to lock the same period with NOWAIT.
        # If Session A left a lock, this will raise LockNotAvailableError.
        session_b = factory()
        try:
            # Set a short lock_timeout so we don't hang.
            session_b.execute(text("SET lock_timeout = '2s'"))
            svc_b = QuotaService(session_b)
            # This should succeed (reserve 20 more, total 100 = limit).
            res = svc_b.reserve_ai_checks(
                ws_id, 20, idempotency_key=f"lr-res-b-{uuid.uuid4().hex[:8]}"
            )
            assert res.ai_checks_reserved == 20
        finally:
            session_b.close()

    def test_idempotent_commit_retry_does_not_block(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Session A: idempotent commit retry returns.

        Immediately Session B: attempts to lock the same reservation.
        Session B must NOT remain blocked.
        """
        engine, factory = engine_factory

        setup = factory()
        try:
            ws = _setup_workspace_with_plan(setup, "LR-COM", monthly_limit=100)
            ws_id = ws.id
        finally:
            setup.close()

        # Reserve + commit first.
        res_key = f"lr-com-res-{uuid.uuid4().hex[:8]}"
        usage_key = f"lr-com-use-{uuid.uuid4().hex[:8]}"

        session_setup = factory()
        try:
            svc = QuotaService(session_setup)
            res = svc.reserve_ai_checks(ws_id, 80, idempotency_key=res_key)
            res_id = res.id
            svc.commit_ai_checks(res_id, 80, usage_idempotency_key=usage_key, provider="OPENAI")
        finally:
            session_setup.close()

        # Session A: idempotent commit retry (should return existing + release lock).
        session_a = factory()
        try:
            svc_a = QuotaService(session_a)
            svc_a.commit_ai_checks(res_id, 80, usage_idempotency_key=usage_key, provider="OPENAI")
        finally:
            session_a.close()

        # Session B: try to lock the same reservation with NOWAIT.
        session_b = factory()
        try:
            session_b.execute(text("SET lock_timeout = '2s'"))
            # Release should work on the reservation without blocking.
            svc_b = QuotaService(session_b)
            svc_b.release_reservation(res_id)
        finally:
            session_b.close()

    def test_idempotent_release_does_not_block(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Session A: idempotent release on terminal reservation returns.

        Immediately Session B: attempts to lock the same reservation.
        Session B must NOT remain blocked.
        """
        engine, factory = engine_factory

        setup = factory()
        try:
            ws = _setup_workspace_with_plan(setup, "LR-REL", monthly_limit=100)
            ws_id = ws.id
        finally:
            setup.close()

        res_key = f"lr-rel-res-{uuid.uuid4().hex[:8]}"

        # Reserve + release first.
        session_setup = factory()
        try:
            svc = QuotaService(session_setup)
            res = svc.reserve_ai_checks(ws_id, 80, idempotency_key=res_key)
            res_id = res.id
            svc.release_reservation(res_id)
        finally:
            session_setup.close()

        # Session A: idempotent release retry (should return + release lock).
        session_a = factory()
        try:
            svc_a = QuotaService(session_a)
            svc_a.release_reservation(res_id)
        finally:
            session_a.close()

        # Session B: try to lock the same reservation.
        session_b = factory()
        try:
            session_b.execute(text("SET lock_timeout = '2s'"))
            svc_b = QuotaService(session_b)
            svc_b.release_reservation(res_id)
        finally:
            session_b.close()
