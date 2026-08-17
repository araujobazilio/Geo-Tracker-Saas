"""Audit log model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPrimaryKey
from app.db.types import JSONBType, UUIDType


class AuditLog(UUIDPrimaryKey, Base):
    """Append-only audit trail of important actions.

    AuditLog is NEVER cascade-deleted and is intentionally NOT linked via
    hard FK with cascade to users/workspaces — those may be deleted while
    audit history must survive (regulatory / dispute retention).
    `user_id` and `workspace_id` are plain UUID columns (no FK) so that
    historical audit records remain valid even if the referenced entity
    is removed.

    Audit logs are append-only: there is no `updated_at` column.

    NEVER store passwords, API secrets, or authentication tokens here.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_workspace_action_timestamp", "workspace_id", "action", "created_at"),
        Index("ix_audit_logs_user_action_timestamp", "user_id", "action", "created_at"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONBType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog action={self.action!r} entity={self.entity_type}:{self.entity_id}>"
