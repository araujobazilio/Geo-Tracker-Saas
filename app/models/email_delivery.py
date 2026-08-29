"""EmailDelivery model — transactional outbox for email delivery.

Notification and EmailDelivery are persisted in the SAME local DB
transaction. A Celery worker sends the email later. This guarantees:
business event durable → notification durable → email intent durable
before network I/O.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import EmailDeliveryStatus
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType

if TYPE_CHECKING:
    from app.models.notification import Notification


class EmailDelivery(UUIDPrimaryKey, TimestampMixin, Base):
    """One email delivery intent linked to a Notification.

    ``recipient_email`` snapshots ``User.email`` at notification/outbox
    creation time, providing delivery audit lineage.

    Status lifecycle:
    PENDING → SENDING (claimed under lock) → SENT or FAILED.

    Stale SENDING (worker died) → FAILED (manual retry required).
    This avoids automatic duplicate email.
    """

    __tablename__ = "email_deliveries"
    __table_args__ = (
        UniqueConstraint("notification_id", name="uq_email_deliveries_one_per_notification"),
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    recipient_email: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[EmailDeliveryStatus] = mapped_column(
        String(20), nullable=False, default=EmailDeliveryStatus.PENDING, index=True
    )

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    notification: Mapped[Notification] = relationship(back_populates="email_delivery")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<EmailDelivery notification={self.notification_id} "
            f"status={self.status} recipient={self.recipient_email!r}>"
        )
