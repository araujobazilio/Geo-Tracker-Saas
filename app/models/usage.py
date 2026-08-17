"""Usage event model (AI Check accounting)."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import DECIMAL as SQL_DECIMAL
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import UsageEventType
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType


class UsageEvent(UUIDPrimaryKey, TimestampMixin, Base):
    """Accounting record for AI Checks and other billable events.

    Money is stored as NUMERIC (Decimal) — never binary floating point.
    `cost_usd` is the estimated USD cost of the underlying provider call.

    UsageEvent is NEVER cascade-deleted; it is required for billing
    disputes and cost accounting. See docs/DATABASE.md.
    """

    __tablename__ = "usage_events"

    workspace_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
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

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UsageEvent workspace={self.workspace_id} type={self.event_type} checks={self.ai_checks}>"
