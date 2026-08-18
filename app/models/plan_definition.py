"""Plan definition model — the source of truth for plan limits and features.

A PlanDefinition is a typed, structured description of what a workspace
on a given plan can do. It is NEVER stored as an unstructured JSON blob.

All integer limits have database CHECK constraints preventing negative
values. `monthly_ai_checks` must always be a finite non-negative integer
(never NULL, never -1, never "unlimited").

PlanDefinition is source-independent: AppSumo, Stripe, and Admin grants
all map to a plan_code, which resolves to a PlanDefinition. Entitlement
rules are NOT duplicated across billing integrations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.plan_provider import PlanProvider


class PlanDefinition(UUIDPrimaryKey, TimestampMixin, Base):
    """A commercial or development plan with explicit resource/usage limits.

    All limits are typed columns with CHECK constraints. An empty
    PlanProvider set means NO providers are allowed (not "all providers").
    `monthly_ai_checks` is always a finite integer — unlimited AI usage
    is not supported to protect paid-provider API economics.
    """

    __tablename__ = "plan_definitions"
    __table_args__ = (
        # Resource limits must be non-negative.
        CheckConstraint("max_projects >= 0", name="ck_plan_definitions_max_projects_non_negative"),
        CheckConstraint(
            "max_keywords_per_project >= 0",
            name="ck_plan_definitions_max_keywords_non_negative",
        ),
        CheckConstraint(
            "max_competitors_per_project >= 0",
            name="ck_plan_definitions_max_competitors_non_negative",
        ),
        CheckConstraint(
            "max_team_members >= 0", name="ck_plan_definitions_max_team_members_non_negative"
        ),
        # AI checks must be finite and non-negative (never unlimited).
        CheckConstraint(
            "monthly_ai_checks >= 0", name="ck_plan_definitions_monthly_ai_checks_non_negative"
        ),
        # Scheduled scan interval: NULL means no scheduled scans allowed.
        # If set, must be positive.
        CheckConstraint(
            "min_scheduled_scan_interval_hours IS NULL " "OR min_scheduled_scan_interval_hours > 0",
            name="ck_plan_definitions_scan_interval_positive",
        ),
    )

    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- Resource limits ---
    max_projects: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_keywords_per_project: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_competitors_per_project: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_team_members: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Usage limit (always finite, never unlimited) ---
    monthly_ai_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Scan capabilities ---
    min_scheduled_scan_interval_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence_scans_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_scans_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- Feature flags ---
    white_label_reports: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exports_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    agency_dashboard: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    integrations_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    byok_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    providers: Mapped[list[PlanProvider]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PlanDefinition code={self.code!r} active={self.is_active}>"
