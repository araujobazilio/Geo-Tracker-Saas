"""Quota reservation model — atomic AI Check quota reservations.

A QuotaReservation reserves a number of AI Checks before a scan or
provider call executes. This prevents race conditions where two workers
both see "100 available" and each reserves 80 (total 160 > limit 100).

Lifecycle:
  ACTIVE    → reservation created, quota held
  COMMITTED → all reserved checks have been used (committed == reserved)
  RELEASED  → unused reserved checks released back (scan canceled/failed)
  EXPIRED   → stale reservation expired by cleanup, remaining released

Reservations are never deleted after completion — they are retained for
operational and accounting history.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import QuotaReservationStatus
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType


class QuotaReservation(UUIDPrimaryKey, TimestampMixin, Base):
    """An atomic reservation of AI Checks for a workspace.

    `idempotency_key` is unique — retrying the same reservation returns
    the existing record rather than creating a duplicate. Reusing the
    same key with conflicting parameters raises a conflict error.

    `usage_period_id` permanently binds the reservation to the
    WorkspaceUsagePeriod where quota was originally reserved. This
    ensures that commit/release/expire operations always update the
    correct monthly period, even across UTC month boundaries.

    Constraints:
      - ai_checks_reserved > 0 (must reserve at least 1)
      - ai_checks_committed >= 0
      - ai_checks_committed <= ai_checks_reserved
      - usage_period_id NOT NULL (every reservation belongs to a period)
    """

    __tablename__ = "quota_reservations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_quota_reservations_idempotency_key"),
        CheckConstraint("ai_checks_reserved > 0", name="ck_quota_reservations_reserved_positive"),
        CheckConstraint(
            "ai_checks_committed >= 0", name="ck_quota_reservations_committed_non_negative"
        ),
        CheckConstraint(
            "ai_checks_committed <= ai_checks_reserved",
            name="ck_quota_reservations_committed_le_reserved",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    usage_period_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspace_usage_periods.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    ai_checks_reserved: Mapped[int] = mapped_column(Integer, nullable=False)
    ai_checks_committed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[QuotaReservationStatus] = mapped_column(
        String(20), nullable=False, default=QuotaReservationStatus.ACTIVE, index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<QuotaReservation workspace={self.workspace_id} "
            f"reserved={self.ai_checks_reserved} committed={self.ai_checks_committed} "
            f"status={self.status}>"
        )
