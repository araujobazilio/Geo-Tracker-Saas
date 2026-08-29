"""ProjectScanSchedule model — interval-based scheduled STANDARD scans."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ScheduledScanOutcome
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.scan import Scan
    from app.models.user import User
    from app.models.workspace import Workspace


class ProjectScanSchedule(UUIDPrimaryKey, TimestampMixin, Base):
    """Interval-based schedule for recurring STANDARD scans on a Project.

    Semantics: "Run this project every N hours."

    One schedule per Project (unique constraint on project_id).

    Entitlement gate: ``PlanDefinition.min_scheduled_scan_interval_hours``
    is the feature flag. NULL = scheduled scans unavailable. A positive
    integer is the minimum permitted interval. The schedule's
    ``interval_hours`` must be >= the current effective minimum at
    creation/update time AND at scheduler execution time (entitlements
    can change after schedule creation).

    No catch-up storm: at most ONE due slot is handled per scheduler
    evaluation. ``next_run_at`` is advanced to the first future interval
    boundary after each evaluation.
    """

    __tablename__ = "project_scan_schedules"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_project_scan_schedules_one_per_project"),
        CheckConstraint("interval_hours > 0", name="ck_project_scan_schedules_interval_positive"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    interval_hours: Mapped[int] = mapped_column(Integer, nullable=False)

    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_scan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("scans.id", ondelete="RESTRICT"),
        nullable=True,
    )
    last_outcome: Mapped[ScheduledScanOutcome | None] = mapped_column(String(50), nullable=True)
    last_skip_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    project: Mapped[Project] = relationship()
    workspace: Mapped[Workspace] = relationship()
    created_by: Mapped[User] = relationship()
    last_scan: Mapped[Scan | None] = relationship(
        foreign_keys=[last_scan_id],
        primaryjoin="ProjectScanSchedule.last_scan_id == Scan.id",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ProjectScanSchedule project={self.project_id} "
            f"interval={self.interval_hours}h enabled={self.enabled}>"
        )
