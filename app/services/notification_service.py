"""Notification service — deduplicated notifications + email outbox.

Responsibilities:
- Create deduplicated Notification (unique on user_id + dedup_key).
- Resolve recipient preferences.
- Create EmailDelivery outbox row when applicable (same transaction).
- Dispatch email task AFTER DB commit.

Does NOT know SMTP details — delegates to EmailTransport.
Does NOT consume AI Checks.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import (
    EmailDeliveryStatus,
    NotificationType,
)
from app.core.logging import get_logger
from app.models.email_delivery import EmailDelivery
from app.models.notification import Notification, NotificationPreference
from app.models.user import User
from app.models.workspace import WorkspaceMember
from app.services.email_transport import EmailTransport, build_email_transport

logger = get_logger("app.notifications")


@dataclass(frozen=True)
class NotificationInput:
    """Input for creating a notification."""

    workspace_id: uuid.UUID
    user_id: uuid.UUID
    notification_type: NotificationType
    title: str
    message: str
    dedup_key: str
    project_id: uuid.UUID | None = None
    scan_id: uuid.UUID | None = None
    opportunity_id: uuid.UUID | None = None
    verification_id: uuid.UUID | None = None
    deep_link_path: str | None = None


class NotificationService:
    """Create deduplicated notifications and email outbox entries."""

    def __init__(
        self,
        session: Session,
        *,
        email_transport: EmailTransport | None = None,
    ) -> None:
        self._session = session
        self._transport = email_transport

    def _get_transport(self) -> EmailTransport:
        if self._transport is not None:
            return self._transport
        return build_email_transport()

    def get_or_create_preference(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> NotificationPreference:
        """Get or create default preferences for a user in a workspace."""
        pref = (
            self._session.execute(
                select(NotificationPreference).where(
                    NotificationPreference.workspace_id == workspace_id,
                    NotificationPreference.user_id == user_id,
                )
            )
            .scalars()
            .first()
        )

        if pref is not None:
            return pref

        pref = NotificationPreference(
            workspace_id=workspace_id,
            user_id=user_id,
            email_enabled=True,
            scheduled_scan_summary=True,
            high_priority_opportunities=True,
            verification_outcomes=True,
        )
        self._session.add(pref)
        self._session.flush()
        return pref

    def _should_email(
        self,
        pref: NotificationPreference,
        notification_type: NotificationType,
    ) -> bool:
        """Check if email should be sent for this notification type."""
        if not pref.email_enabled:
            return False
        if notification_type in (
            NotificationType.SCHEDULED_SCAN_COMPLETED,
            NotificationType.SCHEDULED_SCAN_PARTIAL,
            NotificationType.SCHEDULED_SCAN_FAILED,
        ):
            return pref.scheduled_scan_summary
        if notification_type == NotificationType.NEW_HIGH_PRIORITY_OPPORTUNITY:
            return pref.high_priority_opportunities
        if notification_type in (
            NotificationType.VERIFICATION_RESOLVED,
            NotificationType.VERIFICATION_IMPROVED,
            NotificationType.VERIFICATION_REGRESSED,
            NotificationType.VERIFICATION_INCONCLUSIVE,
        ):
            return pref.verification_outcomes
        return False

    def create_notification(
        self,
        inp: NotificationInput,
        *,
        dispatch_email_task: bool = True,
    ) -> Notification | None:
        """Create a deduplicated notification + email outbox row.

        Returns the Notification if created, None if deduped (already exists).
        The email outbox row is created in the same transaction.
        Email task dispatch happens AFTER commit (caller must commit).

        For MEMBER role: email is only created if the user's preference row
        explicitly enables it. For OWNER/ADMIN: default preferences enable email.
        """
        # Check if notification already exists (dedup).
        existing = (
            self._session.execute(
                select(Notification).where(
                    Notification.user_id == inp.user_id,
                    Notification.dedup_key == inp.dedup_key,
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return None

        # Load user for email snapshot.
        user = self._session.get(User, inp.user_id)
        if user is None or not user.is_active:
            return None

        # Check membership is still active.
        member = (
            self._session.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == inp.workspace_id,
                    WorkspaceMember.user_id == inp.user_id,
                )
            )
            .scalars()
            .first()
        )
        if member is None:
            return None

        notification = Notification(
            workspace_id=inp.workspace_id,
            user_id=inp.user_id,
            notification_type=inp.notification_type,
            title=inp.title,
            message=inp.message,
            project_id=inp.project_id,
            scan_id=inp.scan_id,
            opportunity_id=inp.opportunity_id,
            verification_id=inp.verification_id,
            deep_link_path=inp.deep_link_path,
            dedup_key=inp.dedup_key,
        )
        self._session.add(notification)

        # Use a SAVEPOINT around the insert so that a unique-constraint
        # collision (dedup race) only rolls back the SAVEPOINT, not
        # unrelated pending work in the caller's session. The context
        # manager pattern ensures the SAVEPOINT is properly rolled back
        # on error, even if the session enters an error state.
        try:
            with self._session.begin_nested():
                self._session.flush()
        except IntegrityError:
            # Unique constraint caught a race — dedup.
            return None

        # Resolve preferences and create email outbox if applicable.
        pref = self.get_or_create_preference(inp.workspace_id, inp.user_id)

        # MEMBER role: email only if explicitly enabled in preferences.
        # OWNER/ADMIN: email per preference defaults (which are True).
        should_email = self._should_email(pref, inp.notification_type)

        from app.config import get_settings

        settings = get_settings()

        if should_email and settings.email_enabled:
            message_id = f"<{notification.id}@geo-tracker>"
            delivery = EmailDelivery(
                notification_id=notification.id,
                recipient_email=user.email,
                status=EmailDeliveryStatus.PENDING,
                message_id=message_id,
            )
            self._session.add(delivery)
            self._session.flush()

            if dispatch_email_task:
                # Dispatch email task AFTER the transaction commits.
                # We register a post-commit listener.
                self._dispatch_after_commit(delivery.id)

        return notification

    def _dispatch_after_commit(self, delivery_id: uuid.UUID) -> None:
        """Register a post-commit hook to dispatch the email task.

        If the broker is down, the EmailDelivery remains PENDING and
        the outbox sweeper will pick it up later.
        """
        from sqlalchemy import event

        def _after_commit(session: Session) -> None:
            try:
                from app.workers.notification_tasks import send_email_task

                send_email_task.delay(str(delivery_id))
            except Exception:
                logger.warning(
                    "email_dispatch_failed",
                    delivery_id=str(delivery_id),
                )

        event.listen(self._session, "after_commit", _after_commit, once=True)

    def list_active_recipients(self, workspace_id: uuid.UUID) -> list[tuple[WorkspaceMember, User]]:
        """List active workspace members with their user records.

        Used to resolve recipients at notification generation time.
        If membership was removed, the user is not included.
        """
        rows = self._session.execute(
            select(WorkspaceMember, User)
            .join(User, WorkspaceMember.user_id == User.id)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                User.is_active.is_(True),
            )
        ).all()
        return list(rows)  # type: ignore[arg-type]

    def mark_read(
        self,
        workspace_id: uuid.UUID,
        notification_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> Notification | None:
        """Mark a notification as read. Returns the Notification if updated/found.

        Scoped by workspace_id + notification_id + user_id to enforce
        tenant isolation. A user who is a member of two workspaces
        cannot mark their Workspace B notification via a Workspace A
        route.
        """
        notification = (
            self._session.execute(
                select(Notification).where(
                    Notification.id == notification_id,
                    Notification.workspace_id == workspace_id,
                    Notification.user_id == user_id,
                )
            )
            .scalars()
            .first()
        )
        if notification is None:
            return None
        if notification.read_at is None:
            notification.read_at = datetime.now(UTC)
            self._session.flush()
        return notification

    def mark_all_read(self, user_id: uuid.UUID, workspace_id: uuid.UUID) -> int:
        """Mark all unread notifications for a user as read. Returns count."""
        rows = (
            self._session.execute(
                select(Notification).where(
                    Notification.user_id == user_id,
                    Notification.workspace_id == workspace_id,
                    Notification.read_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        now = datetime.now(UTC)
        for n in rows:
            n.read_at = now
        self._session.flush()
        return len(rows)
