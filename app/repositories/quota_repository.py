"""Usage, quota reservation, and workspace usage period repositories."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.quota_reservation import QuotaReservation
from app.models.usage import UsageEvent
from app.models.workspace_usage_period import WorkspaceUsagePeriod


class UsageRepository:
    """Persistence layer for UsageEvent entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, event: UsageEvent) -> UsageEvent:
        self._session.add(event)
        self._session.flush()
        return event

    def get_by_idempotency_key(self, key: str) -> UsageEvent | None:
        return self._session.execute(
            select(UsageEvent).where(UsageEvent.idempotency_key == key)
        ).scalar_one_or_none()


class QuotaReservationRepository:
    """Persistence layer for QuotaReservation entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, reservation_id: uuid.UUID) -> QuotaReservation | None:
        return self._session.get(QuotaReservation, reservation_id)

    def get_by_idempotency_key(self, key: str) -> QuotaReservation | None:
        return self._session.execute(
            select(QuotaReservation).where(QuotaReservation.idempotency_key == key)
        ).scalar_one_or_none()

    def create(self, reservation: QuotaReservation) -> QuotaReservation:
        self._session.add(reservation)
        self._session.flush()
        return reservation

    def list_expired_active(self, now: datetime) -> list[QuotaReservation]:
        """Return ACTIVE reservations whose expires_at has passed."""
        return list(
            self._session.execute(
                select(QuotaReservation).where(
                    QuotaReservation.status == "ACTIVE",
                    QuotaReservation.expires_at.is_not(None),
                    QuotaReservation.expires_at <= now,
                )
            )
            .scalars()
            .all()
        )


class WorkspaceUsagePeriodRepository:
    """Persistence layer for WorkspaceUsagePeriod entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_for_period(
        self, workspace_id: uuid.UUID, period_start: datetime
    ) -> WorkspaceUsagePeriod | None:
        return self._session.execute(
            select(WorkspaceUsagePeriod).where(
                WorkspaceUsagePeriod.workspace_id == workspace_id,
                WorkspaceUsagePeriod.period_start == period_start,
            )
        ).scalar_one_or_none()

    def create(self, period: WorkspaceUsagePeriod) -> WorkspaceUsagePeriod:
        self._session.add(period)
        self._session.flush()
        return period
