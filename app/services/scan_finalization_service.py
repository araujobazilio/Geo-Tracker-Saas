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
from app.core.enums import QuotaReservationStatus, ScanStatus, VerificationOutcome
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
                    self._post_finalize_verification_lifecycle(scan_id, scan.status)
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
                self._post_finalize_verification_lifecycle(scan_id, status)
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

    def _post_finalize_verification_lifecycle(
        self, scan_id: uuid.UUID, scan_status: ScanStatus
    ) -> None:
        """Centralized post-finalization verification lifecycle.

        Called AFTER the Scan terminal state and quota reconciliation
        are durably committed. If this method fails, the already-durable
        Scan/quota state is NOT rolled back — the failure is logged and
        can be reconciled later. Never replays providers.

        FAILED (VERIFICATION scan):
            Terminalize the PENDING OpportunityVerification as
            INCONCLUSIVE / VERIFICATION_SCAN_FAILED.
            No analysis, no AI Checks, no provider calls.

        COMPLETED / PARTIAL (any scan type):
            Run deterministic analysis in a fresh session.
            For VERIFICATION scans, auto-evaluate or terminalize based
            on the durable analysis result.

            If analysis raises an unexpected exception, the
            ScanAnalysisService persists a FAILED record in its separate
            failure transaction. We then load the durable analysis state
            from a FRESH session and, if it is definitively FAILED,
            terminalize the PENDING verification as INCONCLUSIVE /
            ANALYSIS_NOT_COMPLETED.

            If analysis COMPLETED but evaluation raises an ephemeral
            software/database error, the verification MAY remain PENDING
            for local retry — we do NOT mark it INCONCLUSIVE merely
            because application code had a temporary exception.

        STANDARD / CONFIDENCE scans: unchanged behavior (analysis only).
        """
        if scan_status == ScanStatus.FAILED:
            self._terminalize_failed_verification(scan_id)
            return

        if scan_status not in (ScanStatus.COMPLETED, ScanStatus.PARTIAL):
            return

        try:
            from app.services.scan_analysis_service import ScanAnalysisService

            if self._analysis_session_factory is not None:
                ctx = self._analysis_session_factory
                with ctx() as analysis_session:
                    analysis = ScanAnalysisService(
                        analysis_session,
                        failure_session_factory=self._analysis_session_factory,
                    ).analyze(scan_id)
                    self._maybe_auto_evaluate_verification(
                        analysis_session, scan_id, analysis.status
                    )
            else:
                from app.db.session import get_session_factory

                factory = get_session_factory()
                with factory() as analysis_session:
                    analysis = ScanAnalysisService(
                        analysis_session,
                        failure_session_factory=factory,
                    ).analyze(scan_id)
                    self._maybe_auto_evaluate_verification(
                        analysis_session, scan_id, analysis.status
                    )
        except Exception:
            logger.error(
                "auto_analysis_failed",
                scan_id=str(scan_id),
                exc_info=True,
            )
            # Phase 10.3: The analysis service persists a FAILED
            # ScanAnalysis record in its separate failure transaction
            # before re-raising. Load the durable analysis state from a
            # FRESH session and terminalize the PENDING verification if
            # the analysis is definitively FAILED.
            self._reconcile_analysis_failure(scan_id)

    def _terminalize_failed_verification(self, scan_id: uuid.UUID) -> None:
        """Terminalize a PENDING verification whose scan is FAILED.

        Works for both fresh finalization and idempotent re-finalization
        of an already-terminal FAILED scan (self-healing).
        Zero AI Checks, zero provider calls.
        """
        from sqlalchemy import select

        from app.core.enums import ScanType
        from app.models.opportunity import OpportunityVerification

        scan = self._session.get(Scan, scan_id)
        if scan is None or scan.scan_type != ScanType.VERIFICATION:
            return
        if scan.status != ScanStatus.FAILED:
            return
        verification = self._session.execute(
            select(OpportunityVerification).where(
                OpportunityVerification.verification_scan_id == scan_id
            )
        ).scalar_one_or_none()
        if verification is None:
            return
        if verification.outcome != VerificationOutcome.PENDING:
            return
        try:
            from app.services.verification_lifecycle_service import (
                VerificationLifecycleService,
            )

            VerificationLifecycleService(self._session).terminalize_failed_scan(verification.id)
            logger.info(
                "verification_terminalized_failed_scan",
                scan_id=str(scan_id),
                verification_id=str(verification.id),
            )
        except Exception:
            logger.error(
                "verification_terminalize_failed_scan_error",
                scan_id=str(scan_id),
                verification_id=str(verification.id),
                exc_info=True,
            )

    def _reconcile_analysis_failure(self, scan_id: uuid.UUID) -> None:
        """After an analysis exception, inspect durable analysis state.

        The ScanAnalysisService persists a FAILED record in its own
        failure transaction before re-raising. Using a FRESH session,
        load the durable ScanAnalysis. If it is definitively FAILED and
        the scan is a VERIFICATION scan, terminalize the PENDING
        verification as INCONCLUSIVE / ANALYSIS_NOT_COMPLETED.

        This does NOT replay providers or consume AI Checks.
        """
        from app.core.enums import ScanAnalysisStatus, ScanType
        from app.models.analysis import ANALYSIS_VERSION
        from app.repositories.analysis_repository import ScanAnalysisRepository

        scan = self._session.get(Scan, scan_id)
        if scan is None or scan.scan_type != ScanType.VERIFICATION:
            return
        if scan.status not in (ScanStatus.COMPLETED, ScanStatus.PARTIAL):
            return

        # Use a fresh session to read the durable analysis state.
        try:
            if self._analysis_session_factory is not None:
                ctx = self._analysis_session_factory
                with ctx() as fresh_session:
                    analysis = ScanAnalysisRepository(fresh_session).get_by_scan_and_version(
                        scan_id, ANALYSIS_VERSION
                    )
            else:
                from app.db.session import get_session_factory

                factory = get_session_factory()
                with factory() as fresh_session:
                    analysis = ScanAnalysisRepository(fresh_session).get_by_scan_and_version(
                        scan_id, ANALYSIS_VERSION
                    )
        except Exception:
            logger.error(
                "reconcile_analysis_failure_read_error",
                scan_id=str(scan_id),
                exc_info=True,
            )
            return

        if analysis is None or analysis.status != ScanAnalysisStatus.FAILED:
            return

        # Durable analysis is FAILED → terminalize the PENDING verification.
        from sqlalchemy import select

        from app.models.opportunity import OpportunityVerification

        verification = self._session.execute(
            select(OpportunityVerification).where(
                OpportunityVerification.verification_scan_id == scan_id
            )
        ).scalar_one_or_none()
        if verification is None:
            return
        if verification.outcome != VerificationOutcome.PENDING:
            return
        try:
            from app.services.verification_lifecycle_service import (
                VerificationLifecycleService,
            )

            VerificationLifecycleService(self._session).terminalize_analysis_failure(
                verification.id
            )
            logger.info(
                "verification_terminalized_analysis_failure_reconciled",
                scan_id=str(scan_id),
                verification_id=str(verification.id),
            )
        except Exception:
            logger.error(
                "verification_terminalize_analysis_failure_reconciled_error",
                scan_id=str(scan_id),
                verification_id=str(verification.id),
                exc_info=True,
            )

    def _maybe_auto_evaluate_verification(
        self, session: Session, scan_id: uuid.UUID, analysis_status: str
    ) -> None:
        """Phase 10.1 + 10.2: Auto-evaluate or terminalize a VERIFICATION scan.

        Phase 10.1: if the analysis is COMPLETED, find the corresponding
        OpportunityVerification record and evaluate it.  Evaluation
        failure is logged and swallowed — it MUST NOT rollback the
        analysis or repeat providers.  The verification record remains
        PENDING and can be evaluated manually later.

        Phase 10.2: if the analysis is definitively FAILED,
        terminalize the PENDING verification as INCONCLUSIVE /
        ANALYSIS_NOT_COMPLETED so it does not remain PENDING forever.
        """
        from sqlalchemy import select

        from app.core.enums import ScanAnalysisStatus, ScanType
        from app.models.opportunity import OpportunityVerification

        scan = session.get(Scan, scan_id)
        if scan is None or scan.scan_type != ScanType.VERIFICATION:
            return

        verification = session.execute(
            select(OpportunityVerification).where(
                OpportunityVerification.verification_scan_id == scan_id
            )
        ).scalar_one_or_none()
        if verification is None:
            logger.warning(
                "auto_evaluation_no_verification_record",
                scan_id=str(scan_id),
            )
            return
        if verification.outcome != "PENDING":
            return

        # Phase 10.2: analysis definitively FAILED → terminalize.
        if analysis_status == ScanAnalysisStatus.FAILED:
            try:
                from app.services.verification_lifecycle_service import (
                    VerificationLifecycleService,
                )

                VerificationLifecycleService(session).terminalize_analysis_failure(verification.id)
                logger.info(
                    "verification_terminalized_analysis_failed",
                    scan_id=str(scan_id),
                    verification_id=str(verification.id),
                )
            except Exception:
                logger.error(
                    "verification_terminalize_analysis_failed_error",
                    scan_id=str(scan_id),
                    verification_id=str(verification.id),
                    exc_info=True,
                )
            return

        # Phase 10.1: analysis COMPLETED → auto-evaluate.
        if analysis_status != ScanAnalysisStatus.COMPLETED:
            return

        try:
            from app.services.verification_evaluation_service import (
                VerificationEvaluationService,
            )

            VerificationEvaluationService(session).evaluate(verification.id)
            logger.info(
                "auto_evaluation_completed",
                scan_id=str(scan_id),
                verification_id=str(verification.id),
            )
        except Exception:
            logger.error(
                "auto_evaluation_failed",
                scan_id=str(scan_id),
                verification_id=str(verification.id),
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
        # Phase 10.3: finalize() now centralizes all post-finalization
        # verification lifecycle, including FAILED scan terminalization.
        self._finalizer.finalize(scan.id, trigger_analysis=False)
        return True
