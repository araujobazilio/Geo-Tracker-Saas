"""Workspace usage period model — monthly quota state per workspace.

A WorkspaceUsagePeriod row tracks the aggregate AI Check usage for a
workspace during a specific monthly period (UTC calendar month).

This table provides efficient and concurrency-safe quota state. The
immutable UsageEvent table remains the detailed ledger.

Unique constraint on (workspace_id, period_start) ensures one period
row per workspace per month.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType


class WorkspaceUsagePeriod(UUIDPrimaryKey, TimestampMixin, Base):
    """Monthly usage period for a workspace.

    `ai_checks_used` and `ai_checks_reserved` are the authoritative
    aggregate counters. The effective limit comes from the workspace's
    EffectiveEntitlements (resolved from PlanDefinition).

    Available AI Checks = limit - used - reserved
    """

    __tablename__ = "workspace_usage_periods"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "period_start", name="uq_workspace_usage_period_workspace_period"
        ),
        CheckConstraint("ai_checks_used >= 0", name="ck_workspace_usage_periods_used_non_negative"),
        CheckConstraint(
            "ai_checks_reserved >= 0",
            name="ck_workspace_usage_periods_reserved_non_negative",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ai_checks_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ai_checks_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<WorkspaceUsagePeriod workspace={self.workspace_id} "
            f"period={self.period_start} used={self.ai_checks_used} "
            f"reserved={self.ai_checks_reserved}>"
        )
