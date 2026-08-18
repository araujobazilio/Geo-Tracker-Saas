"""Concurrency test: quota cannot be oversubscribed.

This is one of the most important Phase 3 tests. It uses two independent
database sessions to simulate concurrent workers. PostgreSQL row-level
locking (SELECT ... FOR UPDATE) must prevent both workers from
reserving quota that would exceed the limit.

Without proper locking, both workers would see "100 available" and each
reserve 80, resulting in 160 total — a quota violation.

With proper locking, exactly one reservation succeeds and the other is
rejected. Final reserved <= 100.
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import Any

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
    user = User(email=f"conc-{uuid.uuid4().hex[:8]}@example.com", password_hash="h")
    ws = Workspace(name="Concurrency WS", workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, ws])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db.flush()

    plan = PlanDefinition(
        code=f"CONC_{ws.hex[:8] if hasattr(ws, 'hex') else uuid.uuid4().hex[:8]}",
        name="Concurrency Test Plan",
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
    db.commit()
    return ws


@pytest.mark.integration
class TestQuotaConcurrency:
    def test_concurrent_reservations_cannot_oversubscribe(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Two workers concurrently reserve 80 each against a limit of 100.

        Exactly one should succeed; the other should be rejected.
        Final reserved must be <= 100, never 160.
        """
        # Setup workspace + plan using a committed session (not the
        # transactional test session, since workers need to see the data).
        engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True)
        factory = sessionmaker(
            bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
        )

        setup_session = factory()
        try:
            ws = _setup_workspace_with_plan(setup_session, monthly_limit=100)
            ws_id = ws.id
        finally:
            setup_session.close()

        results: dict[str, Any] = {"success": 0, "rejected": 0, "errors": []}
        lock = threading.Lock()

        def worker_attempt(label: str) -> None:
            session = factory()
            try:
                svc = QuotaService(session)
                svc.reserve_ai_checks(
                    ws_id,
                    requested_checks=80,
                    idempotency_key=f"conc-{label}",
                )
                with lock:
                    results["success"] += 1
            except Exception as e:
                with lock:
                    results["rejected"] += 1
                    results["errors"].append(str(e))
            finally:
                session.close()

        # Run two workers in parallel.
        t1 = threading.Thread(target=worker_attempt, args=("a",))
        t2 = threading.Thread(target=worker_attempt, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        engine.dispose()

        # Exactly one should succeed.
        assert results["success"] == 1, f"Expected 1 success, got {results['success']}"
        assert results["rejected"] == 1, f"Expected 1 rejection, got {results['rejected']}"

        # Verify final state: reserved <= 100.
        verify_session = factory()
        try:
            svc = QuotaService(verify_session)
            snapshot = svc.get_usage_snapshot(ws_id)
            assert snapshot.reserved <= 100, f"Reserved {snapshot.reserved} exceeds limit 100"
            assert snapshot.reserved == 80  # the one that succeeded
        finally:
            verify_session.close()
            engine.dispose()
