"""Quota service — atomic AI Check quota reservations and usage accounting.

This service is the single point of quota enforcement. It uses
PostgreSQL row-level locking (SELECT ... FOR UPDATE) to prevent
race conditions where concurrent workers overspend quota.

Monthly quota period: UTC calendar month (e.g. 2026-08-01 to 2026-09-01).

Available AI Checks = limit - used - reserved

Redis is NOT the quota source of truth. PostgreSQL is authoritative.

Lock ordering (to minimize deadlocks):

  For new reservations:
    1. WorkspaceUsagePeriod (FOR UPDATE)
    2. Create QuotaReservation

  For existing reservation mutations (commit/release/expire):
    1. QuotaReservation (FOR UPDATE)
    2. WorkspaceUsagePeriod (FOR UPDATE, via reservation.usage_period_id)

  This ordering is consistent — no circular lock dependencies.

Each QuotaReservation is permanently bound to the WorkspaceUsagePeriod
where quota was originally reserved (usage_period_id). Commit, release,
and expire operations always update the ORIGINAL period, never the
current month's period. This ensures correct accounting across UTC
month boundaries.

Transaction lifecycle: QuotaService owns its transaction boundaries.
Public operations finish their transactions before returning by default.
The Scan Engine's explicit commit_transaction=False path is the sole
exception: it composes quota accounting into a larger evidence transaction,
and that caller must commit or roll back immediately.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.entitlements import UsageSnapshot
from app.core.enums import CostSource, QuotaReservationStatus, UsageEventType
from app.core.exceptions import ConflictError, QuotaExceededError
from app.core.logging import get_logger
from app.models.quota_reservation import QuotaReservation
from app.models.usage import UsageEvent
from app.models.workspace_usage_period import WorkspaceUsagePeriod
from app.repositories.project_repository import ProjectRepository
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

    Transaction error handling: on any exception (QuotaExceededError,
    ConflictError, IntegrityError), the session is rolled back so it
    is left in a usable state with no held locks or partial mutations.

    Transaction lifecycle defaults to service-owned commit/rollback. The
    Scan Engine may explicitly compose usage into its evidence transaction;
    that caller then owns immediate commit/rollback.
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
        self._project_repo = ProjectRepository(session)

    @property
    def _now(self) -> datetime:
        return self._clock or datetime.now(UTC)

    def _get_or_create_period(
        self, workspace_id: uuid.UUID, period_start: datetime, period_end: datetime
    ) -> WorkspaceUsagePeriod:
        """Get or create the usage period row, using upsert to avoid races."""
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

    def _get_or_create_period_for_update(
        self, workspace_id: uuid.UUID, period_start: datetime, period_end: datetime
    ) -> WorkspaceUsagePeriod:
        """Upsert period, then SELECT ... FOR UPDATE with populate_existing.

        This ensures the returned ORM instance contains CURRENT row
        values after the lock is acquired, not a stale pre-lock snapshot.
        """
        self._get_or_create_period(workspace_id, period_start, period_end)
        period = self._period_repo.get_for_period_for_update(workspace_id, period_start)
        if period is None:  # pragma: no cover
            raise RuntimeError("Failed to lock usage period")
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

    def _validate_project_workspace(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID | None
    ) -> None:
        """Validate that project_id belongs to workspace_id, if provided.

        Fails closed with a ConflictError to prevent cross-workspace
        project linkage in quota reservations.
        """
        if project_id is None:
            return
        project = self._project_repo.get_in_workspace(project_id, workspace_id)
        if project is None:
            raise ConflictError(
                f"Project {project_id} does not belong to workspace {workspace_id}."
            )

    def _validate_reservation_idempotency_match(
        self,
        existing: QuotaReservation,
        workspace_id: uuid.UUID,
        requested_checks: int,
        user_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
    ) -> None:
        """Validate that an existing reservation matches the request parameters.

        Used in both the normal idempotency re-check path and the
        IntegrityError race fallback path. Ensures the same idempotency
        key cannot alias different workspaces or request parameters.

        Raises ConflictError if any parameter differs.
        """
        if (
            existing.workspace_id != workspace_id
            or existing.ai_checks_reserved != requested_checks
            or (existing.user_id or None) != (user_id or None)
            or (existing.project_id or None) != (project_id or None)
        ):
            raise ConflictError("Idempotency key reused with conflicting parameters.")

    def _validate_usage_event_match(
        self,
        existing: UsageEvent,
        reservation_id: uuid.UUID,
        quantity: int,
        provider: str | None,
        model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        cached_input_tokens: int | None,
        cache_write_input_tokens: int | None,
        reasoning_tokens: int | None,
        citation_tokens: int | None,
        search_requests: int | None,
        cost_usd: Decimal | None,
        provider_reported_cost_usd: Decimal | None,
        cost_source: CostSource | None,
        pricing_rule_id: uuid.UUID | None,
        prompt_run_id: uuid.UUID | None,
    ) -> None:
        """Validate that an existing UsageEvent matches the request's material fields.

        Compares all material accounting fields: reservation_id,
        ai_checks, provider, model, tokens, and cost. Used in both the
        normal idempotency re-check path and the IntegrityError race
        fallback path. Ensures contradictory provider cost data is
        never silently discarded.

        Raises ConflictError if any material field differs.
        """
        if (
            existing.quota_reservation_id != reservation_id
            or existing.ai_checks != quantity
            or existing.provider != provider
            or existing.model != model
            or existing.input_tokens != input_tokens
            or existing.output_tokens != output_tokens
            or existing.total_tokens != total_tokens
            or existing.cached_input_tokens != cached_input_tokens
            or existing.cache_write_input_tokens != cache_write_input_tokens
            or existing.reasoning_tokens != reasoning_tokens
            or existing.citation_tokens != citation_tokens
            or existing.search_requests != search_requests
            or existing.cost_usd != cost_usd
            or existing.provider_reported_cost_usd != provider_reported_cost_usd
            or existing.cost_source != cost_source
            or existing.pricing_rule_id != pricing_rule_id
            or existing.prompt_run_id != prompt_run_id
        ):
            raise ConflictError("Usage idempotency key reused with conflicting parameters.")

    def reserve_ai_checks(
        self,
        workspace_id: uuid.UUID,
        requested_checks: int,
        idempotency_key: str,
        user_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        ttl_seconds: int | None = None,
    ) -> QuotaReservation:
        """Atomically reserve AI Checks for a workspace.

        Lock ordering: WorkspaceUsagePeriod → create QuotaReservation.

        Idempotent: retrying with the same idempotency_key returns the
        existing reservation. The idempotency re-check happens AFTER
        acquiring the period lock, so concurrent calls with the same
        key do not leak IntegrityError — one creates, the other returns
        the existing record.

        Transaction lifecycle: the period lock is always released
        (commit or rollback) before this method returns, including on
        idempotent early returns.

        Raises:
            ValueError: if requested_checks <= 0.
            QuotaExceededError: if not enough quota remaining.
            ConflictError: if idempotency_key reused with different parameters,
                or if project_id does not belong to workspace_id.
        """
        if requested_checks <= 0:
            raise ValueError("requested_checks must be positive")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        # Validate project belongs to workspace before any locking.
        self._validate_project_workspace(workspace_id, project_id)

        try:
            ent = self._entitlement_service.get_effective_entitlements(workspace_id)
            limit = ent.monthly_ai_checks

            period_start, period_end = month_period(self._now)
            period = self._get_or_create_period_for_update(workspace_id, period_start, period_end)

            # Re-check idempotency AFTER acquiring the period lock.
            # This prevents two concurrent calls with the same key from
            # both passing the initial check and then one leaking
            # an IntegrityError on the unique constraint.
            existing = self._reservation_repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                self._validate_reservation_idempotency_match(
                    existing, workspace_id, requested_checks, user_id, project_id
                )
                # Release the period lock before returning.
                self._session.commit()
                return existing

            available = limit - period.ai_checks_used - period.ai_checks_reserved
            if requested_checks > available:
                self._audit_record(
                    action="QUOTA_EXCEEDED",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    entity_type="quota",
                    metadata={
                        "requested": requested_checks,
                        "available": available,
                        "limit": limit,
                    },
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

            # Create reservation record, bound to this exact period.
            settings = get_settings()
            reservation_ttl = ttl_seconds or settings.quota_reservation_ttl_seconds
            expires_at = self._now + timedelta(seconds=reservation_ttl)
            reservation = QuotaReservation(
                workspace_id=workspace_id,
                project_id=project_id,
                user_id=user_id,
                usage_period_id=period.id,
                idempotency_key=idempotency_key,
                ai_checks_reserved=requested_checks,
                ai_checks_committed=0,
                status=QuotaReservationStatus.ACTIVE,
                expires_at=expires_at,
            )
            try:
                self._reservation_repo.create(reservation)
            except IntegrityError:
                # Race: another worker inserted the same idempotency_key
                # between our re-check and insert. Roll back the reserved
                # increment, then validate and return the existing
                # reservation using the SAME validation as the normal path.
                self._session.rollback()
                existing = self._reservation_repo.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    self._validate_reservation_idempotency_match(
                        existing, workspace_id, requested_checks, user_id, project_id
                    )
                    return existing
                raise ConflictError("Reservation idempotency conflict.") from None
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

        except (QuotaExceededError, ConflictError):
            self._session.rollback()
            raise
        except IntegrityError:
            self._session.rollback()
            raise

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
        cached_input_tokens: int | None = None,
        cache_write_input_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        citation_tokens: int | None = None,
        search_requests: int | None = None,
        cost_usd: Decimal | None = Decimal("0"),
        provider_reported_cost_usd: Decimal | None = None,
        cost_source: CostSource | None = None,
        pricing_rule_id: uuid.UUID | None = None,
        prompt_run_id: uuid.UUID | None = None,
        commit_transaction: bool = True,
    ) -> UsageEvent:
        """Commit N AI Checks against a reservation.

        Lock ordering: QuotaReservation → WorkspaceUsagePeriod (original).

        Uses the reservation's original usage_period_id, NOT the current
        month. This ensures August reservations committed in September
        update August's counters, not September's.

        Idempotent on usage_idempotency_key: retrying returns the
        existing UsageEvent without double-counting. The idempotency
        re-check happens AFTER acquiring locks and validates ALL
        material accounting fields (reservation, quantity, provider,
        model, tokens, cost).

        By default all locks are released before return. The Scan Engine may
        pass commit_transaction=False to atomically commit evidence and usage;
        in that mode the caller owns the surrounding transaction.

        Raises:
            ValueError: if quantity is invalid.
            ConflictError: if reservation not found, status invalid,
                quantity exceeds remaining, or idempotency key conflict
                (different reservation, quantity, provider, model,
                tokens, or cost).
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        try:
            # Lock the reservation first (lock ordering: reservation → period).
            reservation = self._reservation_repo.get_by_id_for_update(reservation_id)
            if reservation is None:
                raise ConflictError("Reservation not found.")

            # Re-check usage idempotency AFTER acquiring the reservation lock.
            existing_event = self._usage_repo.get_by_idempotency_key(usage_idempotency_key)
            if existing_event is not None:
                # Validate ALL material accounting fields match.
                self._validate_usage_event_match(
                    existing_event,
                    reservation.id,
                    quantity,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    cached_input_tokens,
                    cache_write_input_tokens,
                    reasoning_tokens,
                    citation_tokens,
                    search_requests,
                    cost_usd,
                    provider_reported_cost_usd,
                    cost_source,
                    pricing_rule_id,
                    prompt_run_id,
                )
                if commit_transaction:
                    self._session.commit()
                return existing_event

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

            # Lock the ORIGINAL usage period (not current month).
            period = self._period_repo.get_by_id_for_update(reservation.usage_period_id)
            if period is None:
                raise ConflictError("Original usage period not found.")

            # Transfer from reserved to used on the original period.
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
                cached_input_tokens=cached_input_tokens,
                cache_write_input_tokens=cache_write_input_tokens,
                reasoning_tokens=reasoning_tokens,
                citation_tokens=citation_tokens,
                search_requests=search_requests,
                cost_usd=cost_usd,
                provider_reported_cost_usd=provider_reported_cost_usd,
                cost_source=cost_source,
                pricing_rule_id=pricing_rule_id,
                prompt_run_id=prompt_run_id,
                idempotency_key=usage_idempotency_key,
                quota_reservation_id=reservation.id,
            )
            try:
                self._usage_repo.create(event)
            except IntegrityError:
                # Race: another worker inserted the same usage_idempotency_key.
                # Roll back, fetch the winner, and run THE SAME material
                # payload validation as the normal path.
                self._session.rollback()
                existing_event = self._usage_repo.get_by_idempotency_key(usage_idempotency_key)
                if existing_event is not None:
                    self._validate_usage_event_match(
                        existing_event,
                        reservation.id,
                        quantity,
                        provider,
                        model,
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        cached_input_tokens,
                        cache_write_input_tokens,
                        reasoning_tokens,
                        citation_tokens,
                        search_requests,
                        cost_usd,
                        provider_reported_cost_usd,
                        cost_source,
                        pricing_rule_id,
                        prompt_run_id,
                    )
                    return existing_event
                raise ConflictError("Usage idempotency conflict.") from None
            self._session.flush()
            if commit_transaction:
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

        except (ConflictError, IntegrityError):
            self._session.rollback()
            raise

    def release_reservation(
        self, reservation_id: uuid.UUID, *, commit_transaction: bool = True
    ) -> None:
        """Release remaining uncommitted reserved checks from a reservation.

        Lock ordering: QuotaReservation → WorkspaceUsagePeriod (original).

        Uses the reservation's original usage_period_id, NOT the current
        month. This ensures August reservations released in September
        update August's counters, not September's.

        Idempotent: calling release on a terminal reservation (RELEASED,
        EXPIRED, or COMMITTED) is a no-op. The reservation remains in
        its current terminal state — COMMITTED is never changed to
        RELEASED.

        Transaction lifecycle:
            - ``commit_transaction=True`` (default): all locks are always
              released (commit or rollback) before this method returns,
              including on idempotent early returns. Audit is recorded
              after the commit in a separate session.
            - ``commit_transaction=False``: the caller owns the surrounding
              transaction. The method locks the reservation and the
              original usage period, mutates counters/status, and flushes,
              but does NOT commit and does NOT rollback on success. The
              caller is responsible for immediate commit/rollback. Audit
              is NOT recorded in this mode (the caller records its own
              audit after committing). Terminal reservation no-ops also
              respect ``commit_transaction`` — no internal commit occurs.
        """
        try:
            # Lock the reservation first.
            reservation = self._reservation_repo.get_by_id_for_update(reservation_id)
            if reservation is None:
                raise ConflictError("Reservation not found.")

            # COMMITTED, RELEASED, and EXPIRED are all terminal.
            # Releasing a terminal reservation is an idempotent no-op.
            # COMMITTED must NOT become RELEASED — the reservation was
            # fully consumed and its history must be preserved.
            if reservation.status in (
                QuotaReservationStatus.COMMITTED,
                QuotaReservationStatus.RELEASED,
                QuotaReservationStatus.EXPIRED,
            ):
                # Release the reservation lock before returning.
                if commit_transaction:
                    self._session.commit()
                # In caller-owned mode, do not commit; the caller owns the
                # transaction and will commit or rollback as needed.
                return

            remaining = reservation.ai_checks_reserved - reservation.ai_checks_committed
            if remaining > 0:
                # Lock the ORIGINAL usage period.
                period = self._period_repo.get_by_id_for_update(reservation.usage_period_id)
                if period is None:
                    raise ConflictError("Original usage period not found.")
                period.ai_checks_reserved -= remaining
                reservation.status = QuotaReservationStatus.RELEASED
            else:
                # All reserved checks were committed; preserve the COMMITTED
                # status rather than marking it RELEASED.
                reservation.status = QuotaReservationStatus.COMMITTED
            self._session.flush()
            if commit_transaction:
                self._session.commit()
                self._audit_record(
                    action="QUOTA_RELEASED",
                    workspace_id=reservation.workspace_id,
                    user_id=reservation.user_id,
                    entity_type="quota_reservation",
                    entity_id=reservation.id,
                    metadata={"released": remaining},
                )
            # In caller-owned mode, leave the transaction open for the
            # caller to commit atomically with its own state changes.

        except ConflictError:
            self._session.rollback()
            raise

    def expire_stale_reservations(self) -> int:
        """Expire ACTIVE reservations whose TTL has passed.

        Uses SELECT ... FOR UPDATE SKIP LOCKED so multiple cleanup
        workers can process stale reservations concurrently without
        overlapping. Each worker only processes reservations it has
        locked.

        Each reservation is expired against its ORIGINAL usage period
        (via usage_period_id), not the current month.

        Transaction lifecycle: the transaction is always finished
        (commit or rollback) before returning, even when zero
        reservations are processed. A missing referenced usage period
        is treated as accounting corruption — the method raises and
        rolls back rather than silently continuing.

        Returns the number of reservations expired.
        """
        try:
            now = self._now
            stale = self._reservation_repo.list_expired_active_for_update_skip_locked(now)
            count = 0
            for reservation in stale:
                remaining = reservation.ai_checks_reserved - reservation.ai_checks_committed
                if remaining > 0:
                    # Lock the ORIGINAL usage period for this reservation.
                    period = self._period_repo.get_by_id_for_update(reservation.usage_period_id)
                    if period is None:
                        # Period was deleted — accounting corruption.
                        # Raise rather than silently continuing, since
                        # usage_period_id has a RESTRICT FK and a
                        # missing period indicates data integrity failure.
                        raise ConflictError(
                            f"Original usage period {reservation.usage_period_id} "
                            f"not found for reservation {reservation.id} — "
                            f"possible accounting corruption."
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

            # Always commit to release any FOR UPDATE SKIP LOCKED locks,
            # even when count == 0 (the SKIP LOCKED query may have
            # acquired locks on rows that were then skipped).
            self._session.flush()
            self._session.commit()

            return count

        except Exception:
            self._session.rollback()
            raise

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
