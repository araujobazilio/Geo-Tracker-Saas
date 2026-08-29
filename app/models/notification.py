"""Notification and NotificationPreference models.

Notifications are persisted in PostgreSQL — email is NOT the source of
truth. If SMTP is down, the customer notification still exists in the
database. Email delivery is handled via a transactional outbox pattern
(see ``EmailDelivery`` model).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import NotificationType
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType

if TYPE_CHECKING:
    from app.models.email_delivery import EmailDelivery
    from app.models.opportunity import Opportunity, OpportunityVerification
    from app.models.project import Project
    from app.models.scan import Scan
    from app.models.user import User
    from app.models.workspace import Workspace


class Notification(UUIDPrimaryKey, TimestampMixin, Base):
    """A persisted in-app notification event.

    Deduplication: ``dedup_key`` is unique per ``(user_id, dedup_key)``.
    Repeated finalization/analysis/evaluation must not create duplicate
    notifications — the unique constraint is the final defense.

    Email delivery is linked via ``EmailDelivery`` (1:1, nullable).
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "dedup_key", name="uq_notifications_user_dedup_key"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    notification_type: Mapped[NotificationType] = mapped_column(
        String(50), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(String(2000), nullable=False)

    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )
    scan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("scans.id", ondelete="SET NULL"),
        nullable=True,
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("opportunities.id", ondelete="SET NULL"),
        nullable=True,
    )
    verification_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("opportunity_verifications.id", ondelete="SET NULL"),
        nullable=True,
    )

    deep_link_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workspace: Mapped[Workspace] = relationship()
    user: Mapped[User] = relationship()
    project: Mapped[Project | None] = relationship()
    scan: Mapped[Scan | None] = relationship()
    opportunity: Mapped[Opportunity | None] = relationship()
    verification: Mapped[OpportunityVerification | None] = relationship()
    email_delivery: Mapped[EmailDelivery | None] = relationship(
        back_populates="notification",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Notification type={self.notification_type} "
            f"user={self.user_id} dedup={self.dedup_key!r}>"
        )


class NotificationPreference(UUIDPrimaryKey, TimestampMixin, Base):
    """Per-user, per-workspace notification preferences.

    Defaults for active workspace members:
    - email_enabled = True
    - scheduled_scan_summary = True
    - high_priority_opportunities = True
    - verification_outcomes = True

    Users can disable them. No marketing email in Phase 11.
    """

    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "user_id", name="uq_notification_preferences_workspace_user"
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    email_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scheduled_scan_summary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    high_priority_opportunities: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    verification_outcomes: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<NotificationPreference workspace={self.workspace_id} "
            f"user={self.user_id} email={self.email_enabled}>"
        )
