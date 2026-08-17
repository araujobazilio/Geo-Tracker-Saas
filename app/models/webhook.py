"""Generic provider webhook event model (idempotency foundation)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import WebhookEventStatus
from app.db.base import Base
from app.db.mixins import UUIDPrimaryKey
from app.db.types import JSONBType


class ProviderWebhookEvent(UUIDPrimaryKey, Base):
    """Persistent record of an inbound provider webhook event.

    Supports idempotency: `external_event_id` + `provider` are unique
    together, so a replayed webhook is detected and not re-processed.
    Webhook events are NEVER cascade-deleted (audit / dispute retention).

    Append-only lifecycle: status transitions from RECEIVED → PROCESSED
    (or FAILED / IGNORED). `received_at` and `processed_at` are tracked
    explicitly; there is no generic `updated_at`.
    """

    __tablename__ = "provider_webhook_events"
    __table_args__ = (
        Index(
            "uq_provider_webhook_event_provider_external",
            "provider",
            "external_event_id",
            unique=True,
        ),
    )

    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONBType, nullable=False, default=dict)
    status: Mapped[WebhookEventStatus] = mapped_column(
        String(20), nullable=False, default=WebhookEventStatus.RECEIVED, index=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ProviderWebhookEvent provider={self.provider!r} "
            f"external={self.external_event_id!r} status={self.status}>"
        )
