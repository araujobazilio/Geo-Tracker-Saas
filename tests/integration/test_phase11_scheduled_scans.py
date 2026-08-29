"""Phase 11 integration tests — scheduled scans, notifications, email delivery.

Run all tests TOGETHER in one pytest invocation to avoid repeated
Alembic schema recreation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    NotificationType,
    ProjectStatus,
    ScanStatus,
    ScanType,
    WorkspaceRole,
    WorkspaceType,
)
from app.models.billing import BillingAccount
from app.models.email_delivery import EmailDelivery
from app.models.notification import Notification
from app.models.plan_definition import PlanDefinition
from app.models.project import Project
from app.models.scan import Scan
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.services.email_transport import MemoryEmailTransport
from app.services.notification_service import NotificationInput, NotificationService
from app.services.scanning.dispatcher import CeleryScanDispatcher
from app.services.scheduled_scan_service import ScheduledScanService

# --- Helpers ---


def _create_workspace_with_plan(
    db: Session,
    *,
    monthly_limit: int = 1000,
    min_schedule_interval: int | None = 24,
) -> tuple[Workspace, User]:
    suffix = uuid.uuid4().hex
    user = User(email=f"p11-{suffix}@example.test", password_hash="synthetic")
    workspace = Workspace(name=f"P11 ws {suffix}", workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, workspace])
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER))
    plan = PlanDefinition(
        code=f"P11_{suffix}",
        name="P11 plan",
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
    db.add(plan)
    db.add(
        BillingAccount(
            workspace_id=workspace.id,
            source=BillingSource.ADMIN,
            status=BillingAccountStatus.ACTIVE,
            plan_code=plan.code,
            is_primary=True,
        )
    )
    db.flush()
    return workspace, user


def _create_project(db: Session, workspace: Workspace) -> Project:
    suffix = uuid.uuid4().hex
    project = Project(
        workspace_id=workspace.id,
        name=f"Project {suffix}",
        domain=f"p11-{suffix}.test",
        brand_name="Acme",
        brand_aliases=[],
        target_country="US",
        target_language="en",
        status=ProjectStatus.ACTIVE,
        prompt_input_revision=1,
    )
    db.add(project)
    db.flush()
    return project


def _build_schedule_service(db: Session) -> ScheduledScanService:
    return ScheduledScanService(
        db,
        dispatcher=CeleryScanDispatcher(),
        audit_service=None,
    )


# --- Tests: Schedule creation and entitlement ---


def test_schedule_creation_denied_when_no_entitlement(db_session: Session) -> None:
    """min_scheduled_scan_interval_hours = NULL → schedule creation denied."""
    ws, user = _create_workspace_with_plan(db_session, min_schedule_interval=None)
    project = _create_project(db_session, ws)
    service = _build_schedule_service(db_session)

    from app.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        service.create_or_update_schedule(
            workspace_id=ws.id,
            project_id=project.id,
            enabled=True,
            interval_hours=24,
            created_by_user_id=user.id,
        )


def test_schedule_creation_denied_when_interval_below_minimum(db_session: Session) -> None:
    """Plan minimum = 24h, request 12h → denied."""
    ws, user = _create_workspace_with_plan(db_session, min_schedule_interval=24)
    project = _create_project(db_session, ws)
    service = _build_schedule_service(db_session)

    from app.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        service.create_or_update_schedule(
            workspace_id=ws.id,
            project_id=project.id,
            enabled=True,
            interval_hours=12,
            created_by_user_id=user.id,
        )


def test_schedule_creation_allowed_when_interval_at_minimum(db_session: Session) -> None:
    """Plan minimum = 24h, request 24h → allowed."""
    ws, user = _create_workspace_with_plan(db_session, min_schedule_interval=24)
    project = _create_project(db_session, ws)
    service = _build_schedule_service(db_session)

    schedule = service.create_or_update_schedule(
        workspace_id=ws.id,
        project_id=project.id,
        enabled=True,
        interval_hours=24,
        created_by_user_id=user.id,
    )
    assert schedule.id is not None
    assert schedule.interval_hours == 24
    assert schedule.enabled is True
    assert schedule.next_run_at > datetime.now(UTC)


def test_schedule_creation_with_first_run_at(db_session: Session) -> None:
    """first_run_at is respected."""
    ws, user = _create_workspace_with_plan(db_session, min_schedule_interval=24)
    project = _create_project(db_session, ws)
    service = _build_schedule_service(db_session)

    first_run = datetime.now(UTC) + timedelta(hours=48)
    schedule = service.create_or_update_schedule(
        workspace_id=ws.id,
        project_id=project.id,
        enabled=True,
        interval_hours=24,
        created_by_user_id=user.id,
        first_run_at=first_run,
    )
    assert schedule.next_run_at == first_run


# --- Tests: No catch-up storm ---
# Moved to tests/integration/test_phase11_1.py (requires committed PostgreSQL setup)


# --- Tests: Schedule advance ---
# Moved to tests/integration/test_phase11_1.py (requires committed PostgreSQL setup)


# --- Tests: Notification dedup ---


def test_notification_dedup(db_session: Session) -> None:
    """Repeated notification creation with same dedup_key → one notification."""
    ws, user = _create_workspace_with_plan(db_session)
    transport = MemoryEmailTransport()
    service = NotificationService(db_session, email_transport=transport)

    inp = NotificationInput(
        workspace_id=ws.id,
        user_id=user.id,
        notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
        title="Scan completed",
        message="Test message",
        dedup_key="scheduled-scan:test-scan-id:terminal",
    )

    n1 = service.create_notification(inp, dispatch_email_task=False)
    db_session.commit()
    assert n1 is not None

    n2 = service.create_notification(inp, dispatch_email_task=False)
    db_session.commit()
    assert n2 is None  # Deduped.

    # Verify only one notification exists.
    notifications = (
        db_session.execute(select(Notification).where(Notification.user_id == user.id))
        .scalars()
        .all()
    )
    assert len(notifications) == 1


# --- Tests: Email preference ---


def test_email_disabled_preference_no_email(db_session: Session) -> None:
    """email_enabled=false → in-app notification exists, no email sent."""
    ws, user = _create_workspace_with_plan(db_session)
    transport = MemoryEmailTransport()
    service = NotificationService(db_session, email_transport=transport)

    # Disable email preference.
    pref = service.get_or_create_preference(ws.id, user.id)
    pref.email_enabled = False
    db_session.commit()

    inp = NotificationInput(
        workspace_id=ws.id,
        user_id=user.id,
        notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
        title="Scan completed",
        message="Test message",
        dedup_key="scheduled-scan:test-scan-id-2:terminal",
    )
    notification = service.create_notification(inp, dispatch_email_task=False)
    db_session.commit()

    assert notification is not None

    # No email delivery created.
    deliveries = (
        db_session.execute(
            select(EmailDelivery).where(EmailDelivery.notification_id == notification.id)
        )
        .scalars()
        .all()
    )
    assert len(deliveries) == 0

    # In-app notification exists.
    assert notification.read_at is None


# --- Tests: Email transport ---


def test_memory_email_transport(db_session: Session) -> None:
    """MemoryEmailTransport stores sent emails correctly."""
    transport = MemoryEmailTransport()
    result = transport.send(
        recipient="test@example.com",
        subject="Test subject",
        text_body="Test body",
        html_body="<p>Test body</p>",
        from_address="noreply@geo-tracker.local",
        from_name="GEO Tracker",
        message_id="<test-id@geo-tracker>",
    )
    assert result.success is True
    assert result.message_id == "<test-id@geo-tracker>"
    assert len(transport.sent) == 1
    assert transport.sent[0]["recipient"] == "test@example.com"
    assert transport.sent[0]["subject"] == "Test subject"


# --- Tests: Removed member ---


def test_removed_member_no_notification(db_session: Session) -> None:
    """User no longer WorkspaceMember → no notification."""
    ws, user = _create_workspace_with_plan(db_session)
    service = NotificationService(db_session)

    # Remove membership.
    member = (
        db_session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == ws.id,
                WorkspaceMember.user_id == user.id,
            )
        )
        .scalars()
        .first()
    )
    assert member is not None
    db_session.delete(member)
    db_session.commit()

    inp = NotificationInput(
        workspace_id=ws.id,
        user_id=user.id,
        notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
        title="Scan completed",
        message="Test message",
        dedup_key="scheduled-scan:test-scan-id-3:terminal",
    )
    notification = service.create_notification(inp, dispatch_email_task=False)
    db_session.commit()

    assert notification is None  # No notification for removed member.


# --- Tests: Notification preferences ---


def test_default_preferences(db_session: Session) -> None:
    """Default preferences are all True."""
    ws, user = _create_workspace_with_plan(db_session)
    service = NotificationService(db_session)
    pref = service.get_or_create_preference(ws.id, user.id)
    db_session.commit()

    assert pref.email_enabled is True
    assert pref.scheduled_scan_summary is True
    assert pref.high_priority_opportunities is True
    assert pref.verification_outcomes is True


def test_update_preferences(db_session: Session) -> None:
    """Preferences can be updated."""
    ws, user = _create_workspace_with_plan(db_session)
    service = NotificationService(db_session)
    pref = service.get_or_create_preference(ws.id, user.id)
    pref.email_enabled = False
    pref.scheduled_scan_summary = False
    db_session.commit()

    db_session.expire_all()
    pref2 = service.get_or_create_preference(ws.id, user.id)
    assert pref2.email_enabled is False
    assert pref2.scheduled_scan_summary is False
    assert pref2.high_priority_opportunities is True  # Unchanged.


# --- Tests: Mark read ---


def test_mark_read(db_session: Session) -> None:
    """Mark notification as read."""
    ws, user = _create_workspace_with_plan(db_session)
    service = NotificationService(db_session)

    inp = NotificationInput(
        workspace_id=ws.id,
        user_id=user.id,
        notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
        title="Scan completed",
        message="Test message",
        dedup_key="scheduled-scan:test-scan-id-4:terminal",
    )
    notification = service.create_notification(inp, dispatch_email_task=False)
    db_session.commit()
    assert notification is not None

    assert service.mark_read(ws.id, notification.id, user.id) is not None
    db_session.commit()

    db_session.expire_all()
    n = db_session.get(Notification, notification.id)
    assert n is not None
    assert n.read_at is not None


# --- Tests: Scan schedule lineage ---


def test_scan_schedule_lineage_columns(db_session: Session) -> None:
    """Scan model has scan_schedule_id and scheduled_for columns."""
    scan = Scan(
        workspace_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        prompt_set_id=uuid.uuid4(),
        scan_type=ScanType.STANDARD,
        status=ScanStatus.PENDING,
        idempotency_key="test-key",
        prompt_count=1,
        provider_count=1,
        planned_ai_checks=1,
    )
    assert scan.scan_schedule_id is None
    assert scan.scheduled_for is None


# --- Tests: Schedule disable ---


def test_schedule_disable(db_session: Session) -> None:
    """Disabling a schedule sets enabled=False."""
    ws, user = _create_workspace_with_plan(db_session, min_schedule_interval=24)
    project = _create_project(db_session, ws)
    service = _build_schedule_service(db_session)

    schedule = service.create_or_update_schedule(
        workspace_id=ws.id,
        project_id=project.id,
        enabled=True,
        interval_hours=24,
        created_by_user_id=user.id,
    )
    assert schedule.enabled is True

    disabled = service.disable_schedule(ws.id, project.id)
    assert disabled is not None
    assert disabled.enabled is False


def test_disabled_schedule_not_due(db_session: Session) -> None:
    """Disabled schedule is not claimed as due."""
    ws, user = _create_workspace_with_plan(db_session, min_schedule_interval=24)
    project = _create_project(db_session, ws)
    service = _build_schedule_service(db_session)

    service.create_or_update_schedule(
        workspace_id=ws.id,
        project_id=project.id,
        enabled=True,
        interval_hours=24,
        created_by_user_id=user.id,
        first_run_at=datetime.now(UTC) - timedelta(hours=1),
    )
    service.disable_schedule(ws.id, project.id)

    now = datetime.now(UTC)
    due = service.claim_due_schedules(now=now)
    assert len(due) == 0


# --- Tests: Schedule one per project ---


def test_one_schedule_per_project(db_session: Session) -> None:
    """Creating a second schedule for the same project updates the first."""
    ws, user = _create_workspace_with_plan(db_session, min_schedule_interval=24)
    project = _create_project(db_session, ws)
    service = _build_schedule_service(db_session)

    s1 = service.create_or_update_schedule(
        workspace_id=ws.id,
        project_id=project.id,
        enabled=True,
        interval_hours=24,
        created_by_user_id=user.id,
    )
    s2 = service.create_or_update_schedule(
        workspace_id=ws.id,
        project_id=project.id,
        enabled=True,
        interval_hours=48,
        created_by_user_id=user.id,
    )
    assert s1.id == s2.id
    assert s2.interval_hours == 48
