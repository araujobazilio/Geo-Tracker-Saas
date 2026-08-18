"""Quota service — atomic AI Check quota reservations and usage accounting.

This service is the single point of quota enforcement. It uses
PostgreSQL row-level locking (SELECT ... FOR UPDATE) to prevent
race conditions where concurrent workers overspend quota.

Monthly quota period: UTC calendar month (e.g. 2026-08-01 to 2026-09-01).

Available AI Checks = limit - used - reserved

Redis is NOT the quota source of truth. PostgreSQL is authoritative.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.entitlements import UsageSnapshot
from app.core.enums import QuotaReservationStatus, UsageEventType
from app.core.exceptions import ConflictError, QuotaExceededError
from app.core.logging import get_logger
from app.models.quota_reservation import QuotaReservation
from app.models.usage import UsageEvent
from app.models.workspace_usage_period import WorkspaceUsagePeriod
from app.repositories.quota_repository import (
    QuotaReservationRepository,
    UsageRepository,
    WorkspaceUsagePeriodRepository,
)
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService

logger = get_logger("app.quota")


def month_period(at: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the (start, end) of the UTC calendar month containing `at`.

    If `at` is None, uses the current UTC time.
    """
    now = at or datetime.now(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    return start, end


class QuotaService:
    """Atomic AI Check quota management.

    All reservation/commit/release operations control their own
    transaction boundaries. Audit logging is independent — accounting
    consistency never depends on audit success.
    """

    def __init__(
        self,
        session: Session,
        session_factory: sessionmaker[Session] | None = None,
        audit_service: AuditService | None = None,
        clock: datetime | None = None,
    ) -> None:
        self._session = session
        self._session_factory = session_factory
        self._audit = audit_service
        self._clock = clock
        self._entitlement_service = EntitlementService(session)
        self._period_repo = WorkspaceUsagePeriodRepository(session)
        self._reservation_repo = QuotaReservationRepository(session)
        self._usage_repo = UsageRepository(session)

    @property
    def _now(self) -> datetime:
        return self._clock or datetime.now(UTC)

    def _get_or_create_period(
        self, workspace_id: uuid.UUID, period_start: datetime, period_end: datetime
    ) -> WorkspaceUsagePeriod:
        """Get or create the usage period row, using upsert to avoid races."""
        # Try INSERT ... ON CONFLICT DO NOTHING, then SELECT.
        self._session.execute(
            text(
                "INSERT INTO workspace_usage_periods "
                "(id, workspace_id, period_start, period_end, ai_checks_used, ai_checks_reserved) "
                "VALUES (:id, :ws, :ps, :pe, 0, 0) "
                "ON CONFLICT (workspace_id, period_start) DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "ws": str(workspace_id),
                "ps": period_start,
                "pe": period_end,
            },
        )
        self._session.flush()
        period = self._period_repo.get_for_period(workspace_id, period_start)
        if period is None:  # pragma: no cover
            raise RuntimeError("Failed to get or create usage period")
        return period

    def get_usage_snapshot(self, workspace_id: uuid.UUID) -> UsageSnapshot:
        """Return the current monthly usage snapshot for a workspace."""
        ent = self._entitlement_service.get_effective_entitlements(workspace_id)
        period_start, period_end = month_period(self._now)
        period = self._period_repo.get_for_period(workspace_id, period_start)
        used = period.ai_checks_used if period else 0
        reserved = period.ai_checks_reserved if period else 0
        return UsageSnapshot(
            workspace_id=workspace_id,
            period_start=period_start,
            period_end=period_end,
            limit=ent.monthly_ai_checks,
            used=used,
            reserved=reserved,
        )

    def reserve_ai_checks(
        self,
        workspace_id: uuid.UUID,
        requested_checks: int,
        idempotency_key: str,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
    ) -> QuotaReservation:
        """Atomically reserve AI Checks for a workspace.

        Uses SELECT ... FOR UPDATE on the usage period row to prevent
        concurrent oversubscription. Idempotent: retrying with the same
        idempotency_key returns the existing reservation.

        Raises:
            QuotaExceededError: if not enough quota remaining.
            ConflictError: if idempotency_key reused with different parameters.
        """
        if requested_checks <= 0:
            raise ValueError("requested_checks must be positive")

        # Check idempotency: return existing reservation if same key.
        existing = self._reservation_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if (
                existing.workspace_id != workspace_id
                or existing.ai_checks_reserved != requested_checks
                or (existing.user_id or None) != (user_id or None)
                or (existing.project_id or None) != (project_id or None)
            ):
                raise ConflictError("Idempotency key reused with conflicting parameters.")
            return existing

        ent = self._entitlement_service.get_effective_entitlements(workspace_id)
        limit = ent.monthly_ai_checks

        period_start, period_end = month_period(self._now)
        period = self._get_or_create_period(workspace_id, period_start, period_end)

        # Lock the period row for the duration of this transaction.
        self._session.execute(
            text("SELECT id FROM workspace_usage_periods WHERE id = :pid FOR UPDATE"),
            {"pid": str(period.id)},
        )

        available = limit - period.ai_checks_used - period.ai_checks_reserved
        if requested_checks > available:
            self._audit_record(
                action="QUOTA_EXCEEDED",
                workspace_id=workspace_id,
                user_id=user_id,
                entity_type="quota",
                metadata={"requested": requested_checks, "available": available, "limit": limit},
            )
            raise QuotaExceededError(
                f"AI Check quota exceeded. "
                f"Limit: {limit}, Used: {period.ai_checks_used}, "
                f"Reserved: {period.ai_checks_reserved}, "
                f"Requested: {requested_checks}."
            )

        # Increment reserved count.
        period.ai_checks_reserved += requested_checks
        self._session.flush()

        # Create reservation record.
        settings = get_settings()
        expires_at = self._now + timedelta(seconds=settings.quota_reservation_ttl_seconds)
        reservation = QuotaReservation(
            workspace_id=workspace_id,
            project_id=project_id,
            user_id=user_id,
            idempotency_key=idempotency_key,
            ai_checks_reserved=requested_checks,
            ai_checks_committed=0,
            status=QuotaReservationStatus.ACTIVE,
            expires_at=expires_at,
        )
        self._reservation_repo.create(reservation)
        self._session.commit()

        self._audit_record(
            action="QUOTA_RESERVED",
            workspace_id=workspace_id,
            user_id=user_id,
            entity_type="quota_reservation",
            entity_id=reservation.id,
            metadata={"ai_checks": requested_checks},
        )

        return reservation

    def commit_ai_checks(
        self,
        reservation_id: uuid.UUID,
        quantity: int,
        usage_idempotency_key: str,
        provider: str | None = None,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        cost_usd: Decimal = Decimal("0"),
    ) -> UsageEvent:
        """Commit N AI Checks against a reservation.

        Atomically:
        - Decrements reserved, increments used on the usage period.
        - Increments committed on the reservation.
        - If committed == reserved, marks reservation as COMMITTED.
        - Creates an immutable UsageEvent.

        Idempotent on usage_idempotency_key: retrying returns the
        existing UsageEvent without double-counting.

        Raises:
            ValueError: if quantity is invalid.
            ConflictError: if quantity exceeds remaining uncommitted.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        # Idempotency check on usage event.
        existing_event = self._usage_repo.get_by_idempotency_key(usage_idempotency_key)
        if existing_event is not None:
            return existing_event

        reservation = self._reservation_repo.get_by_id(reservation_id)
        if reservation is None:
            raise ConflictError("Reservation not found.")

        if reservation.status not in (
            QuotaReservationStatus.ACTIVE,
            QuotaReservationStatus.COMMITTED,
        ):
            raise ConflictError(
                f"Cannot commit against reservation with status {reservation.status}."
            )

        remaining_uncommitted = reservation.ai_checks_reserved - reservation.ai_checks_committed
        if quantity > remaining_uncommitted:
            raise ConflictError(
                f"Cannot commit {quantity} checks: only {remaining_uncommitted} "
                f"uncommitted remaining on this reservation."
            )

        # Lock the usage period row.
        period_start, period_end = month_period(self._now)
        period = self._get_or_create_period(reservation.workspace_id, period_start, period_end)
        self._session.execute(
            text("SELECT id FROM workspace_usage_periods WHERE id = :pid FOR UPDATE"),
            {"pid": str(period.id)},
        )

        # Transfer from reserved to used.
        period.ai_checks_reserved -= quantity
        period.ai_checks_used += quantity

        # Update reservation.
        reservation.ai_checks_committed += quantity
        if reservation.ai_checks_committed >= reservation.ai_checks_reserved:
            reservation.status = QuotaReservationStatus.COMMITTED

        # Create immutable UsageEvent.
        event = UsageEvent(
            workspace_id=reservation.workspace_id,
            user_id=reservation.user_id,
            project_id=reservation.project_id,
            event_type=UsageEventType.AI_CHECK,
            provider=provider,
            model=model,
            ai_checks=quantity,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            idempotency_key=usage_idempotency_key,
            quota_reservation_id=reservation.id,
        )
        self._usage_repo.create(event)
        self._session.flush()
        self._session.commit()

        self._audit_record(
            action="QUOTA_COMMITTED",
            workspace_id=reservation.workspace_id,
            user_id=reservation.user_id,
            entity_type="quota_reservation",
            entity_id=reservation.id,
            metadata={"ai_checks": quantity},
        )

        return event

    def release_reservation(self, reservation_id: uuid.UUID) -> None:
        """Release remaining uncommitted reserved checks from a reservation.

        Idempotent: calling release twice does not subtract twice.
        Sets reservation status to RELEASED.
        """
        reservation = self._reservation_repo.get_by_id(reservation_id)
        if reservation is None:
            raise ConflictError("Reservation not found.")

        if reservation.status in (QuotaReservationStatus.RELEASED, QuotaReservationStatus.EXPIRED):
            # Already released/expired — idempotent no-op.
            return

        remaining = reservation.ai_checks_reserved - reservation.ai_checks_committed
        if remaining > 0:
            period_start, period_end = month_period(self._now)
            period = self._get_or_create_period(reservation.workspace_id, period_start, period_end)
            self._session.execute(
                text("SELECT id FROM workspace_usage_periods WHERE id = :pid FOR UPDATE"),
                {"pid": str(period.id)},
            )
            period.ai_checks_reserved -= remaining

        reservation.status = QuotaReservationStatus.RELEASED
        self._session.flush()
        self._session.commit()

        self._audit_record(
            action="QUOTA_RELEASED",
            workspace_id=reservation.workspace_id,
            user_id=reservation.user_id,
            entity_type="quota_reservation",
            entity_id=reservation.id,
            metadata={"released": remaining},
        )

    def expire_stale_reservations(self) -> int:
        """Expire ACTIVE reservations whose TTL has passed.

        Returns the number of reservations expired.
        """
        now = self._now
        stale = self._reservation_repo.list_expired_active(now)
        count = 0
        for reservation in stale:
            remaining = reservation.ai_checks_reserved - reservation.ai_checks_committed
            if remaining > 0:
                period_start, period_end = month_period(now)
                period = self._get_or_create_period(
                    reservation.workspace_id, period_start, period_end
                )
                self._session.execute(
                    text("SELECT id FROM workspace_usage_periods WHERE id = :pid FOR UPDATE"),
                    {"pid": str(period.id)},
                )
                period.ai_checks_reserved -= remaining

            reservation.status = QuotaReservationStatus.EXPIRED
            count += 1

            self._audit_record(
                action="QUOTA_EXPIRED",
                workspace_id=reservation.workspace_id,
                user_id=reservation.user_id,
                entity_type="quota_reservation",
                entity_id=reservation.id,
                metadata={"expired": remaining},
            )

        if count > 0:
            self._session.flush()
            self._session.commit()

        return count

    def _audit_record(
        self,
        *,
        action: str,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        metadata: dict[str, int | str] | None = None,
    ) -> None:
        """Record an audit event. Failure does not propagate."""
        if self._audit is None:
            return
        try:
            self._audit.record(
                action=action,
                workspace_id=workspace_id,
                user_id=user_id,
                entity_type=entity_type,
                entity_id=entity_id,
                metadata=metadata,
            )
        except Exception:
            logger.error("quota_audit_failed", action=action)
