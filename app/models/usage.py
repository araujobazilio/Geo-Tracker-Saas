"""Usage event model (AI Check accounting)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import DECIMAL as SQL_DECIMAL
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import UsageEventType
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType


class UsageEvent(UUIDPrimaryKey, TimestampMixin, Base):
    """Accounting record for AI Checks and other billable events.

    Money is stored as NUMERIC (Decimal) — never binary floating point.
    `cost_usd` is the estimated USD cost of the underlying provider call.

    `idempotency_key` is unique when present — a provider-call retry
    must not result in double-counted AI Checks, tokens, or cost.

    `quota_reservation_id` links to the QuotaReservation that reserved
    this usage, for traceability. It is a plain UUID (no FK cascade) so
    that UsageEvent retention is not compromised by reservation deletion.

    UsageEvent is NEVER cascade-deleted; it is required for billing
    disputes and cost accounting. See docs/DATABASE.md.

    Database-level CHECK constraints enforce non-negative accounting
    values. These are declared on the model AND created by the Alembic
    migration so both layers stay in sync.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint("ai_checks >= 0", name="ck_usage_events_ai_checks_non_negative"),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_usage_events_input_tokens_non_negative",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_usage_events_output_tokens_non_negative",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_usage_events_total_tokens_non_negative",
        ),
        CheckConstraint("cost_usd >= 0", name="ck_usage_events_cost_usd_non_negative"),
        # Partial uniqueness on idempotency_key: only enforced when not NULL.
        UniqueConstraint("idempotency_key", name="uq_usage_events_idempotency_key"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[UsageEventType] = mapped_column(String(30), nullable=False, index=True)
    provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ai_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal] = mapped_column(SQL_DECIMAL(12, 6), nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=False, index=True
    )
    quota_reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, nullable=True, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UsageEvent workspace={self.workspace_id} type={self.event_type} checks={self.ai_checks}>"
