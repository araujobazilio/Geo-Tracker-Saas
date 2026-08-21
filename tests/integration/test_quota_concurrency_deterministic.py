"""Deterministic concurrency tests for the quota engine.

These tests use threading.Barrier to force workers into the contention
scenario simultaneously, ensuring the locking is actually exercised.

Tests:
  - 80 + 80 against 100 (deterministic)
  - same idempotency key concurrent reservation
  - same usage_idempotency_key concurrent commit
  - different usage keys concurrent commit (one wins, one ConflictError)
  - concurrent expiration workers (no double release)
"""

from __future__ import annotations

import os
import threading
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    LLMProvider,
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
from app.services.quota_service import QuotaService

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


def _setup_workspace_with_plan(db: Session, monthly_limit: int = 100) -> Workspace:
    user = User(email=f"dconc-{uuid.uuid4().hex[:8]}@example.com", password_hash="h")
    ws = Workspace(name="DetConc WS", workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, ws])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db.flush()

    plan = PlanDefinition(
        code=f"DC_{uuid.uuid4().hex[:8]}",
        name="DetConc Plan",
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
    from datetime import UTC, datetime

    period_start = datetime(2026, 8, 1, tzinfo=UTC)
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


def _count_active_reservations(db: Session, workspace_id: uuid.UUID) -> int:  # type: ignore[no-untyped-def]
    result = db.execute(
        text(
            "SELECT count(*) FROM quota_reservations WHERE workspace_id = :ws AND status = 'ACTIVE'"
        ),
        {"ws": str(workspace_id)},
    ).fetchone()
    return result[0] if result else 0


@pytest.fixture()
def engine_factory():  # type: ignore[no-untyped-def]
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True, pool_size=10)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield engine, factory
    engine.dispose()


@pytest.mark.integration
class TestDeterministicConcurrency:
    def test_80_plus_80_against_100(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Two workers simultaneously reserve 80 against limit 100.

        Uses a Barrier to force both workers to hit the locking point
        at the same time.

        Expected: 1 success, 1 QuotaExceededError.
        Final: used=0, reserved=80, 1 ACTIVE reservation.
        """
        engine, factory = engine_factory

        setup = factory()
        try:
            ws = _setup_workspace_with_plan(setup, monthly_limit=100)
            ws_id = ws.id
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        results: dict[str, Any] = {"success": 0, "rejected": 0, "errors": []}
        lock = threading.Lock()

        def worker(label: str) -> None:
            session = factory()
            try:
                svc = QuotaService(session)
                # Wait at the barrier so both workers proceed simultaneously.
                barrier.wait(timeout=10)
                svc.reserve_ai_checks(ws_id, 80, idempotency_key=f"det-{label}-{ws_id.hex[:4]}")
                with lock:
                    results["success"] += 1
            except QuotaExceededError as e:
                with lock:
                    results["rejected"] += 1
                    results["errors"].append(str(e))
            except Exception as e:
                with lock:
                    results["rejected"] += 1
                    results["errors"].append(f"{type(e).__name__}: {e}")
            finally:
                session.close()

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert results["success"] == 1, (
            f"Expected 1 success, got {results['success']}: {results['errors']}"
        )
        assert results["rejected"] == 1, (
            f"Expected 1 rejection, got {results['rejected']}: {results['errors']}"
        )

        verify = factory()
        try:
            used, reserved = _get_period_counts(verify, ws_id)
            assert reserved == 80, f"Expected reserved=80, got {reserved}"
            assert used == 0
            active_count = _count_active_reservations(verify, ws_id)
            assert active_count == 1, f"Expected 1 ACTIVE reservation, got {active_count}"
        finally:
            verify.close()

    def test_same_idempotency_key_concurrent(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Two workers simultaneously reserve with the SAME idempotency key.

        Expected: only 1 reservation row, reserved incremented once.
        No IntegrityError escapes.
        """
        engine, factory = engine_factory

        setup = factory()
        try:
            ws = _setup_workspace_with_plan(setup, monthly_limit=100)
            ws_id = ws.id
        finally:
            setup.close()

        idem_key = f"same-idem-{uuid.uuid4().hex[:8]}"
        barrier = threading.Barrier(2)
        results: dict[str, Any] = {"success": 0, "errors": [], "reservation_ids": []}
        lock = threading.Lock()

        def worker() -> None:
            session = factory()
            try:
                svc = QuotaService(session)
                barrier.wait(timeout=10)
                res = svc.reserve_ai_checks(ws_id, 80, idempotency_key=idem_key)
                with lock:
                    results["success"] += 1
                    results["reservation_ids"].append(str(res.id))
            except Exception as e:
                with lock:
                    results["errors"].append(f"{type(e).__name__}: {e}")
            finally:
                session.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        # Both should succeed (one creates, other returns existing).
        assert results["success"] == 2, (
            f"Expected 2 successes, got {results['success']}: {results['errors']}"
        )
        assert len(results["errors"]) == 0, f"Unexpected errors: {results['errors']}"

        # Both should have received the same reservation ID.
        assert len(results["reservation_ids"]) == 2
        assert results["reservation_ids"][0] == results["reservation_ids"][1]

        verify = factory()
        try:
            used, reserved = _get_period_counts(verify, ws_id)
            assert reserved == 80, f"Expected reserved=80 (not doubled), got {reserved}"
        finally:
            verify.close()

    def test_same_usage_key_concurrent_commit(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Reserve 80, then two workers concurrently commit 80 with same usage key.

        Expected: 1 UsageEvent, used=80, reserved=0.
        Both retries resolve idempotently. No duplicate cost.
        """
        engine, factory = engine_factory

        setup = factory()
        try:
            ws = _setup_workspace_with_plan(setup, monthly_limit=100)
            ws_id = ws.id
        finally:
            setup.close()

        # Reserve first.
        res_session = factory()
        try:
            svc = QuotaService(res_session)
            res = svc.reserve_ai_checks(ws_id, 80, idempotency_key=f"res-{uuid.uuid4().hex[:8]}")
            res_id = res.id
        finally:
            res_session.close()

        usage_key = f"same-use-{uuid.uuid4().hex[:8]}"
        barrier = threading.Barrier(2)
        results: dict[str, Any] = {"success": 0, "errors": [], "event_ids": []}
        lock = threading.Lock()

        def worker() -> None:
            session = factory()
            try:
                svc = QuotaService(session)
                barrier.wait(timeout=10)
                event = svc.commit_ai_checks(
                    res_id,
                    80,
                    usage_idempotency_key=usage_key,
                    provider="OPENAI",
                    cost_usd=Decimal("0.05"),
                )
                with lock:
                    results["success"] += 1
                    results["event_ids"].append(str(event.id))
            except Exception as e:
                with lock:
                    results["errors"].append(f"{type(e).__name__}: {e}")
            finally:
                session.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert results["success"] == 2, (
            f"Expected 2 successes, got {results['success']}: {results['errors']}"
        )
        assert len(results["errors"]) == 0, f"Unexpected errors: {results['errors']}"
        assert results["event_ids"][0] == results["event_ids"][1]

        verify = factory()
        try:
            used, reserved = _get_period_counts(verify, ws_id)
            assert used == 80, f"Expected used=80, got {used}"
            assert reserved == 0, f"Expected reserved=0, got {reserved}"
        finally:
            verify.close()

    def test_different_usage_keys_concurrent_commit(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Reserve 80, two workers concurrently commit 80 with different keys.

        Expected: 1 succeeds, 1 ConflictError.
        Only 80 total AI Checks recorded.
        """
        engine, factory = engine_factory

        setup = factory()
        try:
            ws = _setup_workspace_with_plan(setup, monthly_limit=100)
            ws_id = ws.id
        finally:
            setup.close()

        # Reserve first.
        res_session = factory()
        try:
            svc = QuotaService(res_session)
            res = svc.reserve_ai_checks(ws_id, 80, idempotency_key=f"res-{uuid.uuid4().hex[:8]}")
            res_id = res.id
        finally:
            res_session.close()

        barrier = threading.Barrier(2)
        results: dict[str, Any] = {"success": 0, "conflict": 0, "errors": []}
        lock = threading.Lock()

        def worker(label: str) -> None:
            session = factory()
            try:
                svc = QuotaService(session)
                barrier.wait(timeout=10)
                svc.commit_ai_checks(
                    res_id,
                    80,
                    usage_idempotency_key=f"diff-use-{label}-{uuid.uuid4().hex[:4]}",
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

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert results["success"] == 1, (
            f"Expected 1 success, got {results['success']}: {results['errors']}"
        )
        assert results["conflict"] == 1, (
            f"Expected 1 conflict, got {results['conflict']}: {results['errors']}"
        )

        verify = factory()
        try:
            used, reserved = _get_period_counts(verify, ws_id)
            assert used == 80, f"Expected used=80, got {used}"
            assert reserved == 0, f"Expected reserved=0, got {reserved}"
        finally:
            verify.close()

    def test_concurrent_expiration_workers(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Create one expired ACTIVE reservation, run two cleanup workers.

        Expected: reservation released once, period reserved never
        negative, one final EXPIRED state. No double release.
        """
        from datetime import UTC, datetime, timedelta

        engine, factory = engine_factory

        setup = factory()
        try:
            ws = _setup_workspace_with_plan(setup, monthly_limit=100)
            ws_id = ws.id
        finally:
            setup.close()

        # Reserve with a clock 2 hours in the past so TTL has expired.
        past = datetime.now(UTC) - timedelta(hours=2)
        res_session = factory()
        try:
            svc = QuotaService(res_session, clock=past)
            res = svc.reserve_ai_checks(ws_id, 80, idempotency_key=f"exp-{uuid.uuid4().hex[:8]}")
            res_id = res.id
        finally:
            res_session.close()

        barrier = threading.Barrier(2)
        results: dict[str, Any] = {"expired_counts": [], "errors": []}
        lock = threading.Lock()

        def worker() -> None:
            session = factory()
            try:
                svc = QuotaService(session)
                barrier.wait(timeout=10)
                count = svc.expire_stale_reservations()
                with lock:
                    results["expired_counts"].append(count)
            except Exception as e:
                with lock:
                    results["errors"].append(f"{type(e).__name__}: {e}")
            finally:
                session.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        # Exactly one worker should expire 1 reservation; the other gets 0.
        total_expired = sum(results["expired_counts"])
        assert total_expired == 1, (
            f"Expected total 1 expiration, got {total_expired}: {results['errors']}"
        )
        assert len(results["errors"]) == 0, f"Unexpected errors: {results['errors']}"

        verify = factory()
        try:
            used, reserved = _get_period_counts(verify, ws_id)
            assert reserved == 0, f"Expected reserved=0, got {reserved}"
            assert reserved >= 0, "Reserved must never be negative"

            # Verify reservation is EXPIRED.
            result = verify.execute(
                text("SELECT status FROM quota_reservations WHERE id = :rid"),
                {"rid": str(res_id)},
            ).fetchone()
            assert result is not None
            assert result[0] == "EXPIRED"
        finally:
            verify.close()
