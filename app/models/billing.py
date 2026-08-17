"""Billing models: BillingAccount and AppSumoLicense."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AppSumoLicenseStatus, BillingAccountStatus, BillingSource
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import JSONBType, UUIDType


class BillingAccount(UUIDPrimaryKey, TimestampMixin, Base):
    """Billing / entitlement source for a workspace.

    Independent from User so a workspace can be billed via AppSumo,
    Stripe, or an admin grant without coupling to a single user.
    BillingAccount is NEVER cascade-deleted (financial history retention).
    """

    __tablename__ = "billing_accounts"

    workspace_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source: Mapped[BillingSource] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[BillingAccountStatus] = mapped_column(
        String(20), nullable=False, default=BillingAccountStatus.ACTIVE
    )
    external_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    plan_code: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)

    appsumo_licenses: Mapped[list[AppSumoLicense]] = relationship(back_populates="billing_account")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BillingAccount workspace={self.workspace_id} source={self.source}>"


class AppSumoLicense(UUIDPrimaryKey, TimestampMixin, Base):
    """An AppSumo lifetime-deal license attached to a workspace.

    Has its own model (never stored only on the User) so license lifecycle
    events (activation, upgrade, downgrade, deactivation) can be tracked
    independently. AppSumoLicense is NEVER cascade-deleted (license history
    retention). See docs/APPSUMO.md.
    """

    __tablename__ = "appsumo_licenses"

    workspace_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    billing_account_id: Mapped[str | None] = mapped_column(
        UUIDType, ForeignKey("billing_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    external_license_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    appsumo_plan: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[AppSumoLicenseStatus] = mapped_column(
        String(20), nullable=False, default=AppSumoLicenseStatus.ACTIVE, index=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONBType, nullable=False, default=dict
    )

    billing_account: Mapped[BillingAccount | None] = relationship(back_populates="appsumo_licenses")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AppSumoLicense external={self.external_license_id!r} status={self.status}>"
