"""VerificationLifecycleService — terminal lifecycle integrity for
OpportunityVerification records (Phase 10.2).

A Verification Scan that becomes definitively FAILED (quota failure,
stale recovery, all runs failed) must NOT leave the associated
OpportunityVerification PENDING forever.  PENDING must represent work
that can still reach normal deterministic evaluation, not an
already-dead Scan.

This service provides a focused helper to terminalize a PENDING
verification when its scan has definitively failed.  It:

- Loads the OpportunityVerification for update (row lock).
- Verifies the scan is FAILED and the verification is still PENDING.
- Persists INCONCLUSIVE + VERIFICATION_SCAN_FAILED (or
  ANALYSIS_NOT_COMPLETED for analysis failures).
- Commits atomically.
- Zero AI Checks, zero provider calls, zero UsageEvents.

This service does NOT overload VerificationEvaluationService with
provider execution logic.  It is a small, explicit terminalization
helper.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import ScanStatus, ScanType, VerificationOutcome, VerificationReasonCode
from app.core.logging import get_logger
from app.models.opportunity import OpportunityVerification
from app.models.scan import Scan

logger = get_logger("app.verification_lifecycle")


class VerificationLifecycleService:
    """Terminalize PENDING verifications whose scans have definitively failed."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def terminalize_failed_scan(
        self,
        verification_id: uuid.UUID,
        *,
        reason_code: VerificationReasonCode = VerificationReasonCode.VERIFICATION_SCAN_FAILED,
        message: str | None = None,
    ) -> bool:
        """Terminalize a PENDING verification whose scan is FAILED.

        Returns True if the verification was terminalized, False if it
        was already terminal or the scan is not FAILED.

        Zero AI Checks, zero provider calls.
        """
        verification = self._session.execute(
            select(OpportunityVerification)
            .where(OpportunityVerification.id == verification_id)
            .with_for_update()
        ).scalar_one_or_none()
        if verification is None:
            return False
        if verification.outcome != VerificationOutcome.PENDING:
            return False

        scan = self._session.get(Scan, verification.verification_scan_id)
        if scan is None:
            return False
        if scan.scan_type != ScanType.VERIFICATION:
            return False
        if scan.status != ScanStatus.FAILED:
            return False

        now = datetime.now(UTC)
        verification.outcome = VerificationOutcome.INCONCLUSIVE
        verification.reason_code = reason_code
        verification.evaluation_message = message or (
            "The verification scan failed before evidence could be "
            "collected. The verification measurement could not be "
            "completed."
        )
        verification.evaluated_at = now
        self._session.commit()

        logger.info(
            "verification_terminalized_failed_scan",
            verification_id=str(verification.id),
            scan_id=str(scan.id),
            reason_code=reason_code.value,
        )
        return True

    def terminalize_analysis_failure(
        self,
        verification_id: uuid.UUID,
        *,
        message: str | None = None,
    ) -> bool:
        """Terminalize a PENDING verification whose analysis definitively FAILED.

        Returns True if the verification was terminalized, False if it
        was already terminal or the analysis is not FAILED.
        """
        from app.core.enums import ScanAnalysisStatus
        from app.repositories.analysis_repository import ScanAnalysisRepository

        verification = self._session.execute(
            select(OpportunityVerification)
            .where(OpportunityVerification.id == verification_id)
            .with_for_update()
        ).scalar_one_or_none()
        if verification is None:
            return False
        if verification.outcome != VerificationOutcome.PENDING:
            return False

        scan = self._session.get(Scan, verification.verification_scan_id)
        if scan is None:
            return False
        if scan.scan_type != ScanType.VERIFICATION:
            return False
        if scan.status not in (ScanStatus.COMPLETED, ScanStatus.PARTIAL):
            return False

        analysis = ScanAnalysisRepository(self._session).get_by_scan_and_version(
            scan.id, "deterministic-entity-v1"
        )
        if analysis is None or analysis.status != ScanAnalysisStatus.FAILED:
            return False

        now = datetime.now(UTC)
        verification.outcome = VerificationOutcome.INCONCLUSIVE
        verification.reason_code = VerificationReasonCode.ANALYSIS_NOT_COMPLETED
        verification.evaluation_message = message or (
            "The verification scan analysis failed. The verification "
            "measurement could not be evaluated."
        )
        verification.evaluated_at = now
        self._session.commit()

        logger.info(
            "verification_terminalized_analysis_failure",
            verification_id=str(verification.id),
            scan_id=str(scan.id),
        )
        return True
