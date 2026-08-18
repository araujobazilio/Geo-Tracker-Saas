"""Billing account repository extensions for Phase 3."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import BillingAccount


class BillingAccountRepository:
    """Persistence layer for BillingAccount entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_primary(self, workspace_id: uuid.UUID) -> BillingAccount | None:
        """Return the primary billing account for a workspace, or None."""
        return self._session.execute(
            select(BillingAccount).where(
                BillingAccount.workspace_id == workspace_id,
                BillingAccount.is_primary.is_(True),
            )
        ).scalar_one_or_none()

    def list_for_workspace(self, workspace_id: uuid.UUID) -> list[BillingAccount]:
        """Return all billing accounts for a workspace (including history)."""
        return list(
            self._session.execute(
                select(BillingAccount)
                .where(BillingAccount.workspace_id == workspace_id)
                .order_by(BillingAccount.created_at)
            )
            .scalars()
            .all()
        )

    def create(self, account: BillingAccount) -> BillingAccount:
        self._session.add(account)
        self._session.flush()
        return account
