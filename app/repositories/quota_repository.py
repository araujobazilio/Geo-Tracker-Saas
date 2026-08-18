"""Usage, quota reservation, and workspace usage period repositories.

All mutation-oriented repository methods use SELECT ... FOR UPDATE to
acquire row-level locks, ensuring the returned ORM instance contains
CURRENT row values after the lock is acquired (populate_existing).
"""

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

    def get_by_id_for_update(self, reservation_id: uuid.UUID) -> QuotaReservation | None:
        """Load a reservation with FOR UPDATE lock.

        Uses populate_existing() so the ORM instance reflects the
        current row values after the lock is acquired, not a stale
        pre-lock snapshot.
        """
        result = self._session.execute(
            select(QuotaReservation)
            .where(QuotaReservation.id == reservation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    def get_by_idempotency_key(self, key: str) -> QuotaReservation | None:
        return self._session.execute(
            select(QuotaReservation).where(QuotaReservation.idempotency_key == key)
        ).scalar_one_or_none()

    def get_by_idempotency_key_for_update(self, key: str) -> QuotaReservation | None:
        """Load a reservation by idempotency key with FOR UPDATE lock."""
        result = self._session.execute(
            select(QuotaReservation)
            .where(QuotaReservation.idempotency_key == key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

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

    def list_expired_active_for_update_skip_locked(self, now: datetime) -> list[QuotaReservation]:
        """Return expired ACTIVE reservations with FOR UPDATE SKIP LOCKED.

        This allows multiple cleanup workers to process stale reservations
        concurrently without overlapping. Each worker only gets reservations
        not already locked by another worker.
        """
        return list(
            self._session.execute(
                select(QuotaReservation)
                .where(
                    QuotaReservation.status == "ACTIVE",
                    QuotaReservation.expires_at.is_not(None),
                    QuotaReservation.expires_at <= now,
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
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

    def get_for_period_for_update(
        self, workspace_id: uuid.UUID, period_start: datetime
    ) -> WorkspaceUsagePeriod | None:
        """Load a usage period with FOR UPDATE lock.

        Uses populate_existing() so the ORM instance reflects the
        current row values after the lock is acquired, not a stale
        pre-lock snapshot. This is critical for quota math — we must
        never calculate availability from stale pre-lock values.
        """
        result = self._session.execute(
            select(WorkspaceUsagePeriod)
            .where(
                WorkspaceUsagePeriod.workspace_id == workspace_id,
                WorkspaceUsagePeriod.period_start == period_start,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    def get_by_id_for_update(self, period_id: uuid.UUID) -> WorkspaceUsagePeriod | None:
        """Load a usage period by ID with FOR UPDATE lock.

        Used when a reservation already knows its original period_id.
        """
        result = self._session.execute(
            select(WorkspaceUsagePeriod)
            .where(WorkspaceUsagePeriod.id == period_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    def create(self, period: WorkspaceUsagePeriod) -> WorkspaceUsagePeriod:
        self._session.add(period)
        self._session.flush()
        return period
