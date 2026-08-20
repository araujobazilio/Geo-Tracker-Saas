"""Terminal scan classification, unused quota release, and stale recovery.

Finalization is atomic and self-healing:

* The Scan terminal state, ``Project.last_scan_at``, and unused quota
  release commit in **one** transaction. If quota release fails, the
  Scan does not become terminal.
* Calling ``finalize()`` on an already-terminal Scan is safe and
  reconciles any stranded ACTIVE reservation idempotently — no provider
  calls, no new UsageEvents.
* A terminal Scan always has zero unresolved PromptRuns.
* The PromptRun row count must match the immutable Scan plan; otherwise
  finalization treats this as internal data corruption and rolls back.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.enums import QuotaReservationStatus, ScanStatus
from app.core.exceptions import ConflictError, InfrastructureError
from app.core.logging import get_logger
from app.models.project import Project
from app.models.quota_reservation import QuotaReservation
from app.models.scan import Scan
from app.repositories.scan_repository import PromptRunRepository, ScanRepository
from app.services.audit_service import AuditService
from app.services.quota_service import QuotaService

logger = get_logger("app.scan_finalization")

_TERMINAL_STATUSES = (ScanStatus.COMPLETED, ScanStatus.PARTIAL, ScanStatus.FAILED)


class ScanFinalizationService:
    def __init__(
        self,
        session: Session,
        audit_service: AuditService | None = None,
        *,
        analysis_session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    ) -> None:
        self._session = session
        self._audit = audit_service
        self._scans = ScanRepository(session)
        self._runs = PromptRunRepository(session)
        self._quota = QuotaService(session, audit_service=audit_service)
        self._analysis_session_factory = analysis_session_factory

    def finalize(self, scan_id: uuid.UUID, *, trigger_analysis: bool = True) -> ScanStatus:
        try:
            scan = self._scans.get_for_update(scan_id)
            if scan is None:
                raise ConflictError("Scan not found.")

            if scan.status in _TERMINAL_STATUSES:
                self._reconcile_terminal_scan(scan)
                self._session.commit()
                if trigger_analysis:
                    self._trigger_analysis_if_eligible(scan_id, scan.status)
                return scan.status

            succeeded, failed, unresolved = self._runs.terminal_counts(scan.id)
            if unresolved:
                raise ConflictError("Cannot finalize a Scan with unresolved PromptRuns.")

            total_runs = self._runs.count_by_scan(scan.id)
            if total_runs != scan.planned_ai_checks:
                raise InfrastructureError(
                    f"PromptRun row count ({total_runs}) does not match the Scan plan "
                    f"({scan.planned_ai_checks}); possible data corruption."
                )
            if succeeded + failed != scan.planned_ai_checks:
                raise InfrastructureError(
                    f"Terminal PromptRun count ({succeeded + failed}) does not match "
                    f"the Scan plan ({scan.planned_ai_checks}); possible data corruption."
                )

            completed_at = datetime.now(UTC)
            scan.successful_runs = succeeded
            scan.failed_runs = failed
            scan.completed_at = completed_at
            if succeeded == scan.planned_ai_checks:
                scan.status = ScanStatus.COMPLETED
            elif succeeded > 0:
                scan.status = ScanStatus.PARTIAL
            else:
                scan.status = ScanStatus.FAILED
            if succeeded > 0:
                project = self._session.get(Project, scan.project_id)
                if project is not None:
                    project.last_scan_at = completed_at

            self._release_unused_reservation(scan, commit_transaction=False)
            status = scan.status
            workspace_id = scan.workspace_id
            self._session.commit()
            self._record_audit(scan.id, workspace_id, status)
            if trigger_analysis:
                self._trigger_analysis_if_eligible(scan_id, status)
            return status
        except Exception:
            self._session.rollback()
            raise

    def _reconcile_terminal_scan(self, scan: Scan) -> None:
        """Verify/reconcile quota for an already-terminal Scan.

        If a legacy or inconsistent terminal Scan still has an ACTIVE
        reservation with remaining reserved checks, release them
        idempotently within the caller's transaction. No provider calls,
        no new UsageEvents.
        """
        reservation_id = scan.quota_reservation_id
        if reservation_id is None:
            return
        reservation = self._session.get(QuotaReservation, reservation_id)
        if reservation is None:
            return
        if reservation.status == QuotaReservationStatus.ACTIVE:
            self._quota.release_reservation(reservation_id, commit_transaction=False)

    def _release_unused_reservation(self, scan: Scan, *, commit_transaction: bool) -> None:
        reservation_id = scan.quota_reservation_id
        if reservation_id is None:
            return
        self._quota.release_reservation(reservation_id, commit_transaction=commit_transaction)

    def _record_audit(
        self, scan_id: uuid.UUID, workspace_id: uuid.UUID, status: ScanStatus
    ) -> None:
        if self._audit is None:
            return
        action = {
            ScanStatus.COMPLETED: "SCAN_COMPLETED",
            ScanStatus.PARTIAL: "SCAN_PARTIAL",
            ScanStatus.FAILED: "SCAN_FAILED",
        }[status]
        self._audit.record(
            action=action,
            workspace_id=workspace_id,
            entity_type="scan",
            entity_id=scan_id,
        )

    def _trigger_analysis_if_eligible(self, scan_id: uuid.UUID, status: ScanStatus) -> None:
        """Auto-trigger deterministic analysis after finalization.

        Analysis runs in a fresh session. Analysis failure MUST NOT
        rollback scan completion, change quota, or repeat providers.
        The scan remains terminal regardless of analysis outcome.
        The failure session factory is passed to ScanAnalysisService so
        that unexpected exceptions persist a FAILED record in a separate
        transaction.
        """
        if status not in (ScanStatus.COMPLETED, ScanStatus.PARTIAL):
            return
        try:
            from app.services.scan_analysis_service import ScanAnalysisService

            if self._analysis_session_factory is not None:
                ctx = self._analysis_session_factory
                with ctx() as analysis_session:
                    ScanAnalysisService(
                        analysis_session,
                        failure_session_factory=self._analysis_session_factory,
                    ).analyze(scan_id)
            else:
                from app.db.session import get_session_factory

                factory = get_session_factory()
                with factory() as analysis_session:
                    ScanAnalysisService(
                        analysis_session,
                        failure_session_factory=factory,
                    ).analyze(scan_id)
        except Exception:
            logger.error(
                "auto_analysis_failed",
                scan_id=str(scan_id),
                exc_info=True,
            )


class ScanRecoveryService:
    """Fail stale unresolved work without repeating provider requests.

    Recovers both stale ``RUNNING`` scans (worker began but never
    finished) and stale ``PENDING`` scans (dispatch failed or the
    broker/task was lost under early acknowledgement). Recovery never
    replays provider requests.
    """

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._scans = ScanRepository(session)
        self._runs = PromptRunRepository(session)
        self._finalizer = ScanFinalizationService(session, audit_service)

    def recover_stale_scans(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        before = current - timedelta(seconds=self._settings.scan_stale_after_seconds)
        running_ids = [scan.id for scan in self._scans.list_stale_running(before)]
        pending_ids = [scan.id for scan in self._scans.list_stale_pending(before)]
        scan_ids = running_ids + pending_ids
        recovered = 0
        for scan_id in scan_ids:
            try:
                if self._recover_one(scan_id, before, current):
                    recovered += 1
            except Exception:
                self._session.rollback()
                raise
        return recovered

    def _recover_one(self, scan_id: uuid.UUID, before: datetime, current: datetime) -> bool:
        scan = self._scans.get_for_update(scan_id)
        if scan is None or scan.status not in (ScanStatus.RUNNING, ScanStatus.PENDING):
            self._session.commit()
            return False
        if scan.status == ScanStatus.RUNNING:
            if scan.started_at is None or scan.started_at >= before:
                self._session.commit()
                return False
        else:  # PENDING
            if scan.created_at >= before:
                self._session.commit()
                return False

        self._runs.mark_unresolved_failed(
            scan.id,
            completed_at=current,
            error_message="Worker stopped before evidence was durably recorded.",
        )
        self._session.commit()
        self._finalizer.finalize(scan.id, trigger_analysis=False)
        return True
