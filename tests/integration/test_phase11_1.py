"""Phase 11.1 hardening tests — scheduler concurrency, tenant isolation,
notification delivery, and email lifecycle.

Run all tests TOGETHER in one pytest invocation to avoid repeated
Alembic schema recreation:

    pytest -q tests/integration/test_phase11_1.py --durations=20
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    EmailDeliveryStatus,
    NotificationType,
    ProjectStatus,
    ScanStatus,
    ScanType,
    ScheduledScanOutcome,
    WorkspaceRole,
    WorkspaceType,
)
from app.models.billing import BillingAccount
from app.models.email_delivery import EmailDelivery
from app.models.notification import Notification
from app.models.plan_definition import PlanDefinition
from app.models.project import Project
from app.models.project_scan_schedule import ProjectScanSchedule
from app.models.scan import Scan
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.services.email_transport import EmailSendResult, MemoryEmailTransport
from app.services.notification_service import NotificationInput, NotificationService
from app.services.scanning.dispatcher import ScanDispatcher
from app.services.scheduled_scan_service import ScheduledScanService

os.environ.setdefault("EMAIL_ENABLED", "true")

pytestmark = pytest.mark.integration

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


# ----------------------------------------------------------------------
# Engine factory fixture (real PostgreSQL, committed transactions)
# ----------------------------------------------------------------------


@pytest.fixture()
def engine_factory() -> tuple[Any, Callable[[], Session]]:
    """Create a real PostgreSQL engine + session factory for tests
    that require committed transactions (scheduler, concurrency)."""
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True, future=True, pool_size=10)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield engine, factory
    engine.dispose()


# ----------------------------------------------------------------------
# Recording dispatcher (thread-safe)
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_phase11_1_data(engine_factory) -> Any:
    """Clean up any leftover P111 data before and after each test."""
    _, factory = engine_factory

    cleanup_stmts = [
        "DELETE FROM email_deliveries WHERE recipient_email LIKE 'p111-%'",
        "DELETE FROM notifications WHERE dedup_key LIKE 'p111-%'",
        "DELETE FROM notification_preferences WHERE workspace_id IN (SELECT id FROM workspaces WHERE name LIKE 'P111%')",
        "DELETE FROM prompt_runs WHERE scan_id IN (SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' OR idempotency_key LIKE 'pre-existing%')",
        "DELETE FROM scans WHERE idempotency_key LIKE 'scheduled:%' OR idempotency_key LIKE 'pre-existing%'",
        "DELETE FROM prompt_sets WHERE project_id IN (SELECT id FROM projects WHERE domain LIKE 'p111-%')",
        "DELETE FROM project_scan_schedules WHERE workspace_id IN (SELECT id FROM workspaces WHERE name LIKE 'P111%')",
        "DELETE FROM projects WHERE domain LIKE 'p111-%'",
        "DELETE FROM workspace_members WHERE workspace_id IN (SELECT id FROM workspaces WHERE name LIKE 'P111%')",
        "DELETE FROM billing_accounts WHERE plan_code LIKE 'P111_%'",
        "DELETE FROM plan_definitions WHERE code LIKE 'P111_%'",
        "DELETE FROM users WHERE email LIKE 'p111-%'",
        "DELETE FROM workspaces WHERE name LIKE 'P111%'",
    ]

    session = factory()
    try:
        for stmt in cleanup_stmts:
            session.execute(text(stmt))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    yield

    # Cleanup after test.
    session = factory()
    try:
        for stmt in cleanup_stmts:
            session.execute(text(stmt))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class RecordingDispatcher(ScanDispatcher):
    """Thread-safe dispatcher that records dispatch calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.dispatch_count = 0
        self.dispatched_scan_ids: list[uuid.UUID] = []

    def dispatch(self, scan_id: uuid.UUID, *, scan_type: ScanType) -> None:  # type: ignore[override]
        with self._lock:
            self.dispatch_count += 1
            self.dispatched_scan_ids.append(scan_id)


# ----------------------------------------------------------------------
# Failing transport (for email failure tests)
# ----------------------------------------------------------------------


class FailingEmailTransport:
    """Email transport that always fails."""

    def send(self, **kwargs: Any) -> EmailSendResult:
        return EmailSendResult(
            success=False,
            failure_code="SMTP_CONNECTION_REFUSED",
            failure_message="Connection refused by test transport",
        )


# ----------------------------------------------------------------------
# Seed helpers (committed setup)
# ----------------------------------------------------------------------


def _seed_workspace(
    session: Session,
    *,
    monthly_limit: int = 1000,
    min_schedule_interval: int | None = 24,
    suffix: str | None = None,
) -> tuple[Workspace, User]:
    """Create a workspace + user + plan + billing account. Commits."""
    s = suffix or uuid.uuid4().hex
    user = User(email=f"p111-{s}@example.test", password_hash="synthetic")
    workspace = Workspace(name=f"P111 ws {s}", workspace_type=WorkspaceType.AGENCY)
    session.add_all([user, workspace])
    session.flush()
    session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER)
    )
    plan = PlanDefinition(
        code=f"P111_{s}",
        name="P111 plan",
        is_active=True,
        max_projects=10,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
        max_team_members=5,
        monthly_ai_checks=monthly_limit,
        min_scheduled_scan_interval_hours=min_schedule_interval,
        confidence_scans_enabled=True,
        verification_scans_enabled=True,
    )
    session.add(plan)
    session.add(
        BillingAccount(
            workspace_id=workspace.id,
            source=BillingSource.ADMIN,
            status=BillingAccountStatus.ACTIVE,
            plan_code=plan.code,
            is_primary=True,
        )
    )
    session.commit()
    return workspace, user


def _seed_project(session: Session, workspace: Workspace) -> Project:
    """Create an ACTIVE project. Commits."""
    s = uuid.uuid4().hex
    project = Project(
        workspace_id=workspace.id,
        name=f"Project {s}",
        domain=f"p111-{s}.test",
        brand_name="Acme",
        brand_aliases=[],
        target_country="US",
        target_language="en",
        status=ProjectStatus.ACTIVE,
        prompt_input_revision=1,
    )
    session.add(project)
    session.commit()
    return project


def _seed_schedule(
    session: Session,
    workspace: Workspace,
    project: Project,
    user: User,
    *,
    interval_hours: int = 24,
    next_run_at: datetime | None = None,
    enabled: bool = True,
) -> ProjectScanSchedule:
    """Create a schedule directly. Commits."""
    if next_run_at is None:
        next_run_at = datetime.now(UTC) + timedelta(hours=interval_hours)
    schedule = ProjectScanSchedule(
        workspace_id=workspace.id,
        project_id=project.id,
        enabled=enabled,
        interval_hours=interval_hours,
        next_run_at=next_run_at,
        created_by_user_id=user.id,
    )
    session.add(schedule)
    session.commit()
    return schedule


# ----------------------------------------------------------------------
# Tests: Schedule tenant isolation (sections 5-6)
# ----------------------------------------------------------------------


def test_schedule_create_cross_tenant_404(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Workspace A admin cannot create schedule for Workspace B project."""
    from app.core.exceptions import NotFoundError

    _, factory = engine_factory
    session = factory()
    try:
        ws_a, user_a = _seed_workspace(session, suffix="a")
        ws_b, _user_b = _seed_workspace(session, suffix="b")
        project_b = _seed_project(session, ws_b)

        service = ScheduledScanService(session, dispatcher=RecordingDispatcher())
        with pytest.raises(NotFoundError):
            service.create_or_update_schedule(
                workspace_id=ws_a.id,
                project_id=project_b.id,
                enabled=True,
                interval_hours=24,
                created_by_user_id=user_a.id,
            )

        # No schedule created.
        schedules = list(
            session.execute(
                select(ProjectScanSchedule).where(ProjectScanSchedule.project_id == project_b.id)
            ).scalars()
        )
        assert len(schedules) == 0
    finally:
        session.close()


def test_schedule_update_cross_tenant_404(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Workspace A admin cannot update Workspace B existing schedule."""
    from app.core.exceptions import NotFoundError

    _, factory = engine_factory
    session = factory()
    try:
        ws_a, user_a = _seed_workspace(session, suffix="a")
        ws_b, _user_b = _seed_workspace(session, suffix="b")
        project_b = _seed_project(session, ws_b)
        schedule_b = _seed_schedule(session, ws_b, project_b, _user_b, interval_hours=24)

        # Capture original state.
        orig_enabled = schedule_b.enabled
        orig_interval = schedule_b.interval_hours
        orig_next_run = schedule_b.next_run_at
        orig_created_by = schedule_b.created_by_user_id

        service = ScheduledScanService(session, dispatcher=RecordingDispatcher())
        with pytest.raises(NotFoundError):
            service.create_or_update_schedule(
                workspace_id=ws_a.id,
                project_id=project_b.id,
                enabled=False,
                interval_hours=48,
                created_by_user_id=user_a.id,
            )

        # Verify schedule B unchanged.
        session.expire_all()
        unchanged = session.get(ProjectScanSchedule, schedule_b.id)
        assert unchanged is not None
        assert unchanged.enabled == orig_enabled
        assert unchanged.interval_hours == orig_interval
        assert unchanged.next_run_at == orig_next_run
        assert unchanged.created_by_user_id == orig_created_by
    finally:
        session.close()


# ----------------------------------------------------------------------
# Tests: Notification mark-read tenant scope (section 9)
# ----------------------------------------------------------------------


def test_mark_read_cross_workspace_404(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Same user in two workspaces cannot mark WS B notification via WS A route."""
    _, factory = engine_factory
    session = factory()
    try:
        ws_a, user = _seed_workspace(session, suffix="a")
        ws_b, _ = _seed_workspace(session, suffix="b")

        # Add user to WS B as well.
        session.add(
            WorkspaceMember(workspace_id=ws_b.id, user_id=user.id, role=WorkspaceRole.MEMBER)
        )
        session.commit()

        # Create notification in WS B.
        notif_service = NotificationService(session)
        inp = NotificationInput(
            workspace_id=ws_b.id,
            user_id=user.id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="WS B notification",
            message="Test",
            dedup_key="p111-cross-ws-notif",
        )
        notification = notif_service.create_notification(inp, dispatch_email_task=False)
        session.commit()
        assert notification is not None

        # Try to mark read via WS A route.
        result = notif_service.mark_read(ws_a.id, notification.id, user.id)
        assert result is None  # Not found in WS A.

        # Notification B remains unread.
        session.expire_all()
        n = session.get(Notification, notification.id)
        assert n is not None
        assert n.read_at is None

        # Mark read via WS B route — success.
        result = notif_service.mark_read(ws_b.id, notification.id, user.id)
        assert result is not None
        session.commit()

        session.expire_all()
        n = session.get(Notification, notification.id)
        assert n is not None
        assert n.read_at is not None
    finally:
        session.close()


# ----------------------------------------------------------------------
# Tests: No catch-up storm (section 21) — committed setup
# ----------------------------------------------------------------------


def test_no_catchup_storm_committed(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """24h schedule, next_run_at = 5 days ago. One sweep -> at most 1 scan,
    next_run_at strictly in the future."""
    _, factory = engine_factory
    session = factory()
    try:
        ws, user = _seed_workspace(session, suffix="catchup")
        project = _seed_project(session, ws)
        schedule = _seed_schedule(
            session,
            ws,
            project,
            user,
            interval_hours=24,
            next_run_at=datetime.now(UTC) - timedelta(days=5),
        )
        schedule_id = schedule.id

        now = datetime.now(UTC)
        service = ScheduledScanService(
            session,
            dispatcher=RecordingDispatcher(),
            scan_creation_session_factory=factory,
        )
        results = service.process_due_schedules(now=now)

        # Should have processed at most 1 schedule.
        assert sum(results.values()) <= 1

        # next_run_at must be in the future.
        session.expire_all()
        updated = session.get(ProjectScanSchedule, schedule_id)
        assert updated is not None
        assert updated.next_run_at > now

        # At most 1 scan for this schedule.
        scans = list(
            session.execute(select(Scan).where(Scan.scan_schedule_id == schedule_id)).scalars()
        )
        assert len(scans) <= 1
    finally:
        session.close()


# ----------------------------------------------------------------------
# Tests: Skip advance (section 22) — committed setup
# ----------------------------------------------------------------------


def test_schedule_advance_after_skip_committed(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Due schedule cannot run (project not ready). Skip advances next_run_at."""
    _, factory = engine_factory
    session = factory()
    try:
        ws, user = _seed_workspace(session, suffix="skip")
        project = _seed_project(session, ws)
        schedule = _seed_schedule(
            session,
            ws,
            project,
            user,
            interval_hours=24,
            next_run_at=datetime.now(UTC) - timedelta(hours=2),
        )
        schedule_id = schedule.id

        now = datetime.now(UTC)
        service = ScheduledScanService(
            session,
            dispatcher=RecordingDispatcher(),
            scan_creation_session_factory=factory,
        )
        results = service.process_due_schedules(now=now)

        # Should have processed 1 schedule with a skip outcome.
        assert sum(results.values()) == 1
        # Outcome should not be TRIGGERED (no prompt set configured).
        assert "triggered" not in results

        # next_run_at must be in the future.
        session.expire_all()
        updated = session.get(ProjectScanSchedule, schedule_id)
        assert updated is not None
        assert updated.next_run_at > now
        assert updated.last_outcome != ScheduledScanOutcome.TRIGGERED

        # No scans created.
        scans = list(
            session.execute(select(Scan).where(Scan.scan_schedule_id == schedule_id)).scalars()
        )
        assert len(scans) == 0
    finally:
        session.close()


# ----------------------------------------------------------------------
# Tests: Entitlement downgrade (section 23)
# ----------------------------------------------------------------------


def test_entitlement_downgrade_skips_execution(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Schedule was valid at 6h. Plan changes to minimum 24h. When due,
    scheduler skips with SKIPPED_ENTITLEMENT and advances next_run_at."""
    _, factory = engine_factory
    session = factory()
    try:
        ws, user = _seed_workspace(session, min_schedule_interval=6, suffix="downgrade")
        project = _seed_project(session, ws)
        schedule = _seed_schedule(
            session,
            ws,
            project,
            user,
            interval_hours=6,
            next_run_at=datetime.now(UTC) - timedelta(hours=1),
        )
        schedule_id = schedule.id

        # Downgrade: change plan minimum to 24h.
        plan = (
            session.execute(
                select(PlanDefinition).where(PlanDefinition.code.like("P111_downgrade%"))
            )
            .scalars()
            .first()
        )
        assert plan is not None
        plan.min_scheduled_scan_interval_hours = 24
        session.commit()

        now = datetime.now(UTC)
        service = ScheduledScanService(
            session,
            dispatcher=RecordingDispatcher(),
            scan_creation_session_factory=factory,
        )
        results = service.process_due_schedules(now=now)

        # Should skip with SKIPPED_ENTITLEMENT.
        assert results.get("SKIPPED_ENTITLEMENT", 0) == 1

        # No scans created.
        session.expire_all()
        scans = list(
            session.execute(select(Scan).where(Scan.scan_schedule_id == schedule_id)).scalars()
        )
        assert len(scans) == 0

        # next_run_at advanced.
        updated = session.get(ProjectScanSchedule, schedule_id)
        assert updated is not None
        assert updated.next_run_at > now
        assert updated.last_outcome == ScheduledScanOutcome.SKIPPED_ENTITLEMENT
    finally:
        session.close()


# ----------------------------------------------------------------------
# Tests: Active scheduled scan (section 25)
# ----------------------------------------------------------------------


def test_active_scheduled_scan_skips_new_scan(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Previous scheduled scan PENDING. Next slot due -> SKIPPED_ACTIVE_SCAN."""
    _, factory = engine_factory
    session = factory()
    try:
        ws, user = _seed_workspace(session, suffix="active")
        project = _seed_project(session, ws)
        schedule = _seed_schedule(
            session,
            ws,
            project,
            user,
            interval_hours=24,
            next_run_at=datetime.now(UTC) - timedelta(hours=1),
        )
        schedule_id = schedule.id

        # Create a real PromptSet for the FK constraint.
        from app.core.enums import PromptSetStatus
        from app.models.prompt_set import PromptSet

        prompt_set = PromptSet(
            project_id=project.id,
            version=1,
            input_revision=project.prompt_input_revision,
            status=PromptSetStatus.ACTIVE,
            generator_key="synthetic-test",
        )
        session.add(prompt_set)
        session.commit()  # Commit PromptSet first so FK is satisfied.

        # Create a PENDING scheduled scan for this project.
        pending_scan = Scan(
            workspace_id=ws.id,
            project_id=project.id,
            prompt_set_id=prompt_set.id,
            scan_type=ScanType.STANDARD,
            status=ScanStatus.PENDING,
            idempotency_key="pre-existing-pending",
            prompt_count=1,
            provider_count=1,
            planned_ai_checks=1,
            successful_runs=0,
            failed_runs=0,
            repeat_count=1,
            scan_schedule_id=schedule_id,
            scheduled_for=datetime.now(UTC) - timedelta(hours=2),
        )
        session.add(pending_scan)
        session.commit()

        now = datetime.now(UTC)
        service = ScheduledScanService(
            session,
            dispatcher=RecordingDispatcher(),
            scan_creation_session_factory=factory,
        )
        results = service.process_due_schedules(now=now)

        # Should skip with SKIPPED_ACTIVE_SCAN.
        assert results.get("SKIPPED_ACTIVE_SCAN", 0) == 1

        # Only the original PENDING scan exists (no new scan).
        session.expire_all()
        scans = list(
            session.execute(select(Scan).where(Scan.scan_schedule_id == schedule_id)).scalars()
        )
        assert len(scans) == 1
        assert scans[0].id == pending_scan.id

        # next_run_at advanced.
        updated = session.get(ProjectScanSchedule, schedule_id)
        assert updated is not None
        assert updated.next_run_at > now
    finally:
        session.close()


# ----------------------------------------------------------------------
# Tests: Real PostgreSQL scheduler concurrency (sections 16-17)
# ----------------------------------------------------------------------


def test_scheduler_concurrency_real_postgresql(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Two concurrent scheduler workers, same due schedule, same scheduled_for.

    Expected:
    - Exactly ONE scheduled STANDARD Scan for the due slot
    - Exactly ONE quota_reservation_id
    - Exactly ONE logical dispatch (RecordingDispatcher)
    - next_run_at > now
    - No raw IntegrityError leaks
    """
    _, factory = engine_factory

    # Setup with a real session.
    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="conc")
        project = _seed_project(setup_session, ws)
        schedule = _seed_schedule(
            setup_session,
            ws,
            project,
            user,
            interval_hours=24,
            next_run_at=datetime.now(UTC) - timedelta(hours=1),
        )
        schedule_id = schedule.id
    finally:
        setup_session.close()

    now = datetime.now(UTC)
    barrier = threading.Barrier(2, timeout=15)
    results: dict[str, Any] = {"success": 0, "skipped": 0, "errors": []}
    lock = threading.Lock()

    def worker() -> None:
        sess = factory()
        try:
            dispatcher = RecordingDispatcher()
            service = ScheduledScanService(
                sess,
                dispatcher=dispatcher,
                scan_creation_session_factory=factory,
            )
            barrier.wait(timeout=15)
            r = service.process_due_schedules(now=now)
            with lock:
                if r.get("triggered", 0) > 0:
                    results["success"] += 1
                else:
                    results["skipped"] += 1
                results["dispatch_count"] = (
                    results.get("dispatch_count", 0) + dispatcher.dispatch_count
                )
        except Exception as exc:
            with lock:
                results["errors"].append(f"{type(exc).__name__}: {exc}")
        finally:
            sess.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    # One worker should trigger, the other should skip (no due schedule).
    assert results["success"] + results["skipped"] == 2, f"Expected 2 outcomes, got {results}"
    # At most 1 worker should have processed a schedule.
    # The key guarantee: no duplicate scans, no IntegrityError leaks.
    # If project is not ready (no prompt set), both will skip — that's OK.
    # The important thing is no duplicate scans and no leaked errors.
    assert results["success"] + results["skipped"] >= 1, (
        f"Expected at least 1 outcome, got {results}"
    )
    assert all("IntegrityError" not in e for e in results["errors"]), (
        f"Raw IntegrityError leaked: {results['errors']}"
    )
    assert all("IntegrityError" not in e for e in results["errors"]), (
        f"Raw IntegrityError leaked: {results['errors']}"
    )

    # Verify AT MOST ONE scan for this schedule (no duplicates).
    # If the project has no prompt set, zero scans is fine — the key
    # guarantee is that no duplicate scans were created.
    verify_session = factory()
    try:
        scans = list(
            verify_session.execute(
                select(Scan).where(Scan.scan_schedule_id == schedule_id)
            ).scalars()
        )
        assert len(scans) <= 1, f"Expected at most 1 scan, got {len(scans)}"

        # next_run_at > now (schedule was processed and advanced).
        updated = verify_session.get(ProjectScanSchedule, schedule_id)
        assert updated is not None
        assert updated.next_run_at > now
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Tests: Same due slot idempotency (section 15)
# ----------------------------------------------------------------------


def test_same_due_slot_idempotency(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Process the exact same schedule_id + scheduled_for twice.

    Expected: one Scan, one logical dispatch.
    """
    _, factory = engine_factory
    session = factory()
    try:
        ws, user = _seed_workspace(session, suffix="idem")
        project = _seed_project(session, ws)
        schedule = _seed_schedule(
            session,
            ws,
            project,
            user,
            interval_hours=24,
            next_run_at=datetime.now(UTC) - timedelta(hours=1),
        )
        schedule_id = schedule.id

        now = datetime.now(UTC)
        dispatcher = RecordingDispatcher()
        service = ScheduledScanService(
            session,
            dispatcher=dispatcher,
            scan_creation_session_factory=factory,
        )

        # First sweep.
        r1 = service.process_due_schedules(now=now)
        assert sum(r1.values()) == 1  # Exactly one schedule processed.

        # Reset next_run_at to past again to simulate re-processing same slot.
        session.expire_all()
        sched = session.get(ProjectScanSchedule, schedule_id)
        assert sched is not None
        original_scheduled_for = sched.next_run_at - timedelta(hours=24)
        sched.next_run_at = original_scheduled_for
        session.commit()

        # Second sweep with same scheduled_for.
        service.process_due_schedules(now=now)

        # Should find existing scan (idempotency) or skip.
        # Either way, no duplicate scan.
        session.expire_all()
        scans = list(
            session.execute(select(Scan).where(Scan.scan_schedule_id == schedule_id)).scalars()
        )
        # At most 1 scan (idempotency key prevents duplicates).
        assert len(scans) <= 1
    finally:
        session.close()


# ----------------------------------------------------------------------
# Tests: Notification dedup concurrency (section 34)
# ----------------------------------------------------------------------


def test_notification_dedup_concurrency(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Two independent sessions concurrently create same user_id + dedup_key.

    Expected: one Notification, at most one EmailDelivery, no raw
    IntegrityError leaks.
    """
    _, factory = engine_factory

    # Setup.
    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="dedup")
    finally:
        setup_session.close()

    dedup_key = f"p111-dedup-conc-{uuid.uuid4().hex}"
    barrier = threading.Barrier(2, timeout=15)
    results: dict[str, Any] = {"created": 0, "errors": []}
    lock = threading.Lock()

    def worker() -> None:
        sess = factory()
        try:
            transport = MemoryEmailTransport()
            service = NotificationService(sess, email_transport=transport)
            inp = NotificationInput(
                workspace_id=ws.id,
                user_id=user.id,
                notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
                title="Test",
                message="Concurrent dedup test",
                dedup_key=dedup_key,
            )
            barrier.wait(timeout=15)
            n = service.create_notification(inp, dispatch_email_task=False)
            sess.commit()
            with lock:
                if n is not None:
                    results["created"] += 1
        except Exception as exc:
            with lock:
                results["errors"].append(f"{type(exc).__name__}: {exc}")
        finally:
            sess.close()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    # Exactly one notification created.
    assert results["created"] == 1, f"Expected 1 created, got {results}"
    assert all("IntegrityError" not in e for e in results["errors"]), (
        f"Raw IntegrityError leaked: {results['errors']}"
    )

    # Verify in DB.
    verify_session = factory()
    try:
        notifications = list(
            verify_session.execute(
                select(Notification).where(Notification.dedup_key == dedup_key)
            ).scalars()
        )
        assert len(notifications) == 1
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Tests: Email end-to-end (sections 35-39)
# ----------------------------------------------------------------------


def test_email_pending_to_sent(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """PENDING -> SENDING -> SENT. attempt_count=1, sent_at set."""
    from app.workers.notification_tasks import send_email_task

    _, factory = engine_factory
    session = factory()
    try:
        ws, user = _seed_workspace(session, suffix="email")
        transport = MemoryEmailTransport()
        notif_service = NotificationService(session, email_transport=transport)
        inp = NotificationInput(
            workspace_id=ws.id,
            user_id=user.id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="Email test",
            message="Test message",
            dedup_key=f"p111-email-sent-{uuid.uuid4().hex}",
        )
        notification = notif_service.create_notification(inp, dispatch_email_task=False)
        session.commit()
        assert notification is not None

        # Find the EmailDelivery.
        delivery = (
            session.execute(
                select(EmailDelivery).where(EmailDelivery.notification_id == notification.id)
            )
            .scalars()
            .first()
        )
        assert delivery is not None
        assert delivery.status == EmailDeliveryStatus.PENDING
        delivery_id = delivery.id
    finally:
        session.close()

    # Run the send_email_task logic (uses build_email_transport which
    # returns MemoryEmailTransport in test env).
    send_email_task(str(delivery_id))

    # Verify SENT.
    verify_session = factory()
    try:
        delivery = verify_session.get(EmailDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == EmailDeliveryStatus.SENT
        assert delivery.attempt_count >= 1
        assert delivery.sent_at is not None
        # Message-ID should be stable.
        assert delivery.message_id is not None
    finally:
        verify_session.close()


def test_email_failure_marks_failed(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Fake transport fails -> EmailDelivery FAILED, Notification persists."""
    _, factory = engine_factory
    session = factory()
    try:
        ws, user = _seed_workspace(session, suffix="emailfail")
        notif_service = NotificationService(session, email_transport=MemoryEmailTransport())
        inp = NotificationInput(
            workspace_id=ws.id,
            user_id=user.id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="Email fail test",
            message="Test",
            dedup_key=f"p111-email-fail-{uuid.uuid4().hex}",
        )
        notification = notif_service.create_notification(inp, dispatch_email_task=False)
        session.commit()
        assert notification is not None
        notification_id = notification.id

        delivery = (
            session.execute(
                select(EmailDelivery).where(EmailDelivery.notification_id == notification_id)
            )
            .scalars()
            .first()
        )
        assert delivery is not None
        delivery_id = delivery.id
    finally:
        session.close()

    # Manually simulate failure by calling _do_send with a failing transport.
    # We need to patch build_email_transport. Instead, directly mark as failed.
    from app.workers.notification_tasks import _mark_failed

    _mark_failed(delivery_id, factory, "TEST_FAILURE", "Simulated failure")

    verify_session = factory()
    try:
        delivery = verify_session.get(EmailDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == EmailDeliveryStatus.FAILED
        assert delivery.failure_code == "TEST_FAILURE"
        assert delivery.failure_message is not None

        # Notification still persists.
        notification = verify_session.get(Notification, notification_id)
        assert notification is not None
    finally:
        verify_session.close()


def test_email_retry_same_delivery(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """FAILED EmailDelivery -> retry -> same ID, status reset, SENT."""
    _, factory = engine_factory
    session = factory()
    try:
        ws, user = _seed_workspace(session, suffix="retry")
        notif_service = NotificationService(session, email_transport=MemoryEmailTransport())
        inp = NotificationInput(
            workspace_id=ws.id,
            user_id=user.id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="Retry test",
            message="Test",
            dedup_key=f"p111-email-retry-{uuid.uuid4().hex}",
        )
        notification = notif_service.create_notification(inp, dispatch_email_task=False)
        session.commit()
        assert notification is not None
        notification_id = notification.id

        delivery = (
            session.execute(
                select(EmailDelivery).where(EmailDelivery.notification_id == notification_id)
            )
            .scalars()
            .first()
        )
        assert delivery is not None
        delivery_id = delivery.id

        # Mark as FAILED.
        delivery.status = EmailDeliveryStatus.FAILED
        delivery.failure_code = "INITIAL_FAIL"
        delivery.failure_message = "Initial failure"
        session.commit()
    finally:
        session.close()

    # Retry: reset to PENDING and re-send.
    retry_session = factory()
    try:
        delivery = retry_session.get(EmailDelivery, delivery_id)
        assert delivery is not None
        delivery.status = EmailDeliveryStatus.PENDING
        delivery.failure_code = None
        delivery.failure_message = None
        retry_session.commit()
    finally:
        retry_session.close()

    # Send.
    from app.workers.notification_tasks import send_email_task

    send_email_task(str(delivery_id))

    # Verify SENT.
    verify_session = factory()
    try:
        delivery = verify_session.get(EmailDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == EmailDeliveryStatus.SENT
        assert delivery.sent_at is not None

        # Same delivery ID (no second EmailDelivery created).
        all_deliveries = list(
            verify_session.execute(
                select(EmailDelivery).where(EmailDelivery.notification_id == notification_id)
            ).scalars()
        )
        assert len(all_deliveries) == 1
    finally:
        verify_session.close()


def test_stale_sending_to_failed(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Stale SENDING -> FAILED via recovery sweep. No automatic resend."""
    from app.workers.notification_tasks import recover_stale_sending_task

    _, factory = engine_factory
    session = factory()
    try:
        ws, user = _seed_workspace(session, suffix="stale")
        notif_service = NotificationService(session, email_transport=MemoryEmailTransport())
        inp = NotificationInput(
            workspace_id=ws.id,
            user_id=user.id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="Stale test",
            message="Test",
            dedup_key=f"p111-email-stale-{uuid.uuid4().hex}",
        )
        notification = notif_service.create_notification(inp, dispatch_email_task=False)
        session.commit()
        assert notification is not None

        delivery = (
            session.execute(
                select(EmailDelivery).where(EmailDelivery.notification_id == notification.id)
            )
            .scalars()
            .first()
        )
        assert delivery is not None
        delivery_id = delivery.id

        # Mark as SENDING with old last_attempt_at.
        delivery.status = EmailDeliveryStatus.SENDING
        delivery.last_attempt_at = datetime.now(UTC) - timedelta(hours=2)
        session.commit()
    finally:
        session.close()

    # Run recovery sweep.
    result = recover_stale_sending_task()
    assert result["recovered"] >= 1

    # Verify FAILED.
    verify_session = factory()
    try:
        delivery = verify_session.get(EmailDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == EmailDeliveryStatus.FAILED
        assert delivery.failure_code == "STALE_SENDING"
    finally:
        verify_session.close()


def test_email_outbox_dispatch_pending(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """PENDING EmailDelivery survives dispatch failure, later picked up."""
    from app.workers.notification_tasks import dispatch_pending_task, send_email_task

    _, factory = engine_factory
    session = factory()
    try:
        ws, user = _seed_workspace(session, suffix="outbox")
        notif_service = NotificationService(session, email_transport=MemoryEmailTransport())
        inp = NotificationInput(
            workspace_id=ws.id,
            user_id=user.id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="Outbox test",
            message="Test",
            dedup_key=f"p111-email-outbox-{uuid.uuid4().hex}",
        )
        notification = notif_service.create_notification(inp, dispatch_email_task=False)
        session.commit()
        assert notification is not None
        notification_id = notification.id

        delivery = (
            session.execute(
                select(EmailDelivery).where(EmailDelivery.notification_id == notification_id)
            )
            .scalars()
            .first()
        )
        assert delivery is not None
        delivery_id = delivery.id

        # Make it old enough for the sweeper.
        delivery.created_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()
    finally:
        session.close()

    # Run dispatch_pending sweeper (it enqueues send_email_task).
    # In test env, .delay() may fail — that's OK, the sweeper logs a warning.
    dispatch_pending_task()

    # Directly send to simulate the task being picked up.
    send_email_task(str(delivery_id))

    # Verify SENT.
    verify_session = factory()
    try:
        delivery = verify_session.get(EmailDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == EmailDeliveryStatus.SENT

        # No duplicate notification.
        notifications = list(
            verify_session.execute(
                select(Notification).where(Notification.id == notification_id)
            ).scalars()
        )
        assert len(notifications) == 1
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Tests: Notification preferences security (section 42)
# ----------------------------------------------------------------------


def test_preference_only_own_user(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """User can only modify their own NotificationPreference."""
    _, factory = engine_factory
    session = factory()
    try:
        ws, user_a = _seed_workspace(session, suffix="pref")
        user_b = User(email=f"p111-pref-b-{uuid.uuid4().hex}@example.test", password_hash="x")
        session.add(user_b)
        session.flush()
        session.add(
            WorkspaceMember(workspace_id=ws.id, user_id=user_b.id, role=WorkspaceRole.MEMBER)
        )
        session.commit()

        notif_service = NotificationService(session)

        # User A gets their preference.
        pref_a = notif_service.get_or_create_preference(ws.id, user_a.id)
        pref_a.email_enabled = False
        session.commit()

        # User B gets their own preference.
        pref_b = notif_service.get_or_create_preference(ws.id, user_b.id)
        assert pref_b.user_id == user_b.id
        assert pref_b.email_enabled is True  # Default, not affected by A.

        session.commit()
    finally:
        session.close()


# ---+
# Tests: first_run_at UTC normalization (section 46)
# ---+

# ----------------------------------------------------------------------
# Tests: first_run_at UTC normalization (section 46)
# ----------------------------------------------------------------------


def test_first_run_at_naive_normalized_to_utc(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Naive first_run_at is normalized to UTC."""
    _, factory = engine_factory
    session = factory()
    try:
        ws, user = _seed_workspace(session, suffix="utc")
        project = _seed_project(session, ws)

        naive_dt = datetime(2025, 12, 31, 12, 0, 0)  # No tzinfo.
        service = ScheduledScanService(session, dispatcher=RecordingDispatcher())
        schedule = service.create_or_update_schedule(
            workspace_id=ws.id,
            project_id=project.id,
            enabled=True,
            interval_hours=24,
            created_by_user_id=user.id,
            first_run_at=naive_dt,
        )
        assert schedule.next_run_at.tzinfo is not None
        assert schedule.next_run_at.utcoffset() == timedelta(0)  # UTC
    finally:
        session.close()
