"""OpportunityWorkflowService — status transition management.

Enforces the Phase 9/10 allowed status transitions for Opportunities.

Allowed transitions:
- OPEN → IN_PROGRESS, DISMISSED
- IN_PROGRESS → OPEN, IMPLEMENTED, DISMISSED
- IMPLEMENTED → IN_PROGRESS, DISMISSED
- DISMISSED → OPEN

Forbidden (public PATCH):
- Any → VERIFIED (reserved for Phase 10 system transition)
- VERIFIED → anything (read-only)

Phase 10 additions:
- Transitioning to IMPLEMENTED freezes the implementation baseline
  occurrence (the latest eligible OpportunityOccurrence at that moment).
- Returning to IN_PROGRESS clears the frozen baseline and implemented_at.
- mark_verified_from_verification() performs the internal system-only
  IMPLEMENTED → VERIFIED transition when a VerificationOutcome.RESOLVED
  result is persisted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    OpportunityStatus,
    ScanAnalysisStatus,
    ScanStatus,
    ScanType,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.opportunity import Opportunity, OpportunityOccurrence

logger = get_logger("app.opportunity_workflow")

_ALLOWED_TRANSITIONS: dict[OpportunityStatus, set[OpportunityStatus]] = {
    OpportunityStatus.OPEN: {OpportunityStatus.IN_PROGRESS, OpportunityStatus.DISMISSED},
    OpportunityStatus.IN_PROGRESS: {
        OpportunityStatus.OPEN,
        OpportunityStatus.IMPLEMENTED,
        OpportunityStatus.DISMISSED,
    },
    OpportunityStatus.IMPLEMENTED: {
        OpportunityStatus.IN_PROGRESS,
        OpportunityStatus.DISMISSED,
    },
    OpportunityStatus.DISMISSED: {OpportunityStatus.OPEN},
    OpportunityStatus.VERIFIED: set(),  # read-only
}


class OpportunityWorkflowService:
    """Manage Opportunity status transitions with audit timestamps."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def transition(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        opportunity_id: uuid.UUID,
        new_status: OpportunityStatus,
        dismissal_reason: str | None = None,
    ) -> OpportunityStatus:
        """Transition an Opportunity to a new status.

        Returns the new status. Raises ValidationError if the transition
        is not allowed. Raises NotFoundError if the Opportunity doesn't
        exist or doesn't belong to the workspace/project.
        """
        opp = self._load_opportunity_for_update(workspace_id, project_id, opportunity_id)
        current = opp.status
        if current == new_status:
            return current  # no-op

        if new_status == OpportunityStatus.VERIFIED:
            raise ValidationError("VERIFIED status is reserved for Phase 10 Verification Scans.")

        allowed = _ALLOWED_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            current_val = current.value if hasattr(current, "value") else str(current)
            new_val = new_status.value if hasattr(new_status, "value") else str(new_status)
            raise ValidationError(f"Transition from {current_val} to {new_val} is not allowed.")

        now = datetime.now(UTC)
        self._apply_transition(opp, current, new_status, now, dismissal_reason)
        self._session.commit()
        return new_status

    # ------------------------------------------------------------------
    # Phase 10: system-only VERIFIED transition
    # ------------------------------------------------------------------

    def mark_verified_from_verification(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        opportunity_id: uuid.UUID,
        verification_id: uuid.UUID,
    ) -> OpportunityStatus:
        """System-only transition: IMPLEMENTED → VERIFIED.

        Called by the VerificationEvaluationService after a
        VerificationOutcome.RESOLVED result is persisted.

        Phase 10.2 hardening:
        1. Lock the Opportunity FOR UPDATE before any validation.
        2. Validate the verification record matches ALL of:
           - verification.id == verification_id
           - verification.workspace_id == workspace_id
           - verification.project_id == project_id
           - verification.opportunity_id == opportunity_id
           - verification.outcome == RESOLVED
        3. Validate the verification's baseline_occurrence_id equals
           the Opportunity's CURRENT implementation_baseline_occurrence_id.
           This prevents a RESOLVED verification from an OLD
           implementation cycle from marking a NEW cycle VERIFIED.
        4. Verify the Opportunity is still IMPLEMENTED.

        If any check fails, the RESOLVED verification remains valid
        historical evidence for its cycle, but the Opportunity is NOT
        transitioned to VERIFIED.

        Returns the final status (VERIFIED if transitioned, or the
        current status if validation failed).
        """
        from app.core.enums import VerificationOutcome
        from app.models.opportunity import OpportunityVerification

        # Phase 10.2: lock the Opportunity FIRST, before any
        # validation, to prevent the implementation cycle from
        # changing between validation and transition.
        opp = self._load_opportunity_for_update(workspace_id, project_id, opportunity_id)

        # Full ownership validation: ALL fields must match.
        verification = self._session.execute(
            select(OpportunityVerification).where(
                OpportunityVerification.id == verification_id,
                OpportunityVerification.workspace_id == workspace_id,
                OpportunityVerification.project_id == project_id,
                OpportunityVerification.opportunity_id == opportunity_id,
            )
        ).scalar_one_or_none()
        if verification is None:
            logger.warning(
                "verification_not_found_for_mark_verified",
                opportunity_id=str(opportunity_id),
                verification_id=str(verification_id),
            )
            self._session.commit()
            return opp.status
        if verification.outcome != VerificationOutcome.RESOLVED:
            logger.warning(
                "verification_not_resolved_for_mark_verified",
                opportunity_id=str(opportunity_id),
                verification_id=str(verification_id),
                outcome=verification.outcome.value,
            )
            self._session.commit()
            return opp.status

        # Phase 10.2: validate the verification's baseline_occurrence_id
        # equals the Opportunity's CURRENT frozen baseline.  This
        # prevents a RESOLVED verification from an OLD implementation
        # cycle from marking a NEW cycle VERIFIED.
        if opp.implementation_baseline_occurrence_id is None:
            logger.info(
                "verification_resolved_but_no_current_baseline",
                opportunity_id=str(opportunity_id),
                verification_id=str(verification_id),
            )
            self._session.commit()
            return opp.status
        if verification.baseline_occurrence_id != opp.implementation_baseline_occurrence_id:
            logger.info(
                "verification_resolved_but_baseline_cycle_mismatch",
                opportunity_id=str(opportunity_id),
                verification_id=str(verification_id),
                verification_baseline=str(verification.baseline_occurrence_id),
                current_baseline=str(opp.implementation_baseline_occurrence_id),
            )
            self._session.commit()
            return opp.status

        if opp.status != OpportunityStatus.IMPLEMENTED:
            logger.info(
                "verification_resolved_but_status_changed",
                opportunity_id=str(opportunity_id),
                verification_id=str(verification_id),
                current_status=opp.status.value,
            )
            self._session.commit()
            return opp.status

        opp.status = OpportunityStatus.VERIFIED
        opp.verified_at = datetime.now(UTC)
        self._session.commit()
        return opp.status

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_opportunity_for_update(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        opportunity_id: uuid.UUID,
    ) -> Opportunity:
        opp = self._session.execute(
            select(Opportunity)
            .where(
                Opportunity.id == opportunity_id,
                Opportunity.workspace_id == workspace_id,
                Opportunity.project_id == project_id,
            )
            .with_for_update()
        ).scalar_one_or_none()

        if opp is None:
            raise NotFoundError("Opportunity not found.")
        return opp

    def _apply_transition(
        self,
        opp: Opportunity,
        current: OpportunityStatus,
        new_status: OpportunityStatus,
        now: datetime,
        dismissal_reason: str | None,
    ) -> None:
        opp.status = new_status

        if new_status == OpportunityStatus.IMPLEMENTED:
            opp.implemented_at = now
            self._freeze_implementation_baseline(opp)
        elif new_status == OpportunityStatus.DISMISSED:
            opp.dismissed_at = now
            if dismissal_reason:
                opp.dismissal_reason = dismissal_reason
        elif new_status == OpportunityStatus.OPEN:
            opp.dismissed_at = None
            opp.dismissal_reason = None
            if current == OpportunityStatus.IMPLEMENTED:
                opp.implemented_at = None
                opp.implementation_baseline_occurrence_id = None
        elif new_status == OpportunityStatus.IN_PROGRESS:
            opp.dismissed_at = None
            opp.dismissal_reason = None
            if current == OpportunityStatus.IMPLEMENTED:
                # Re-implementation cycle: clear the frozen baseline.
                opp.implemented_at = None
                opp.implementation_baseline_occurrence_id = None

    def _freeze_implementation_baseline(self, opp: Opportunity) -> None:
        """Freeze the latest ELIGIBLE OpportunityOccurrence as the
        implementation baseline.

        Phase 10.1 hardening: instead of taking the latest occurrence
        and then validating it, we filter for eligible occurrences first
        and select the latest among those.  This prevents freezing an
        ineligible occurrence (e.g., from a CONFIDENCE scan or a
        non-completed analysis) when an earlier eligible one exists.

        Eligibility:
        - belongs to this Opportunity
        - source Scan is STANDARD
        - source Scan is COMPLETED or PARTIAL
        - source ScanAnalysis is COMPLETED

        Fail closed if no eligible occurrence exists — the Opportunity
        cannot be marked IMPLEMENTED without a usable baseline.
        """
        if opp.implementation_baseline_occurrence_id is not None:
            # Already frozen for this implementation cycle.
            return

        from app.models.scan import Scan
        from app.repositories.analysis_repository import ScanAnalysisRepository

        # Join with Scan to order by scan completion time (more reliable
        # than occurrence.created_at which can have identical timestamps
        # when multiple occurrences are created in quick succession).
        occurrences = list(
            self._session.execute(
                select(OpportunityOccurrence)
                .join(Scan, Scan.id == OpportunityOccurrence.scan_id)
                .where(OpportunityOccurrence.opportunity_id == opp.id)
                .order_by(
                    Scan.completed_at.desc().nulls_last(),
                    OpportunityOccurrence.created_at.desc(),
                )
            ).scalars()
        )

        if not occurrences:
            raise ValidationError(
                "Cannot transition to IMPLEMENTED: no OpportunityOccurrence "
                "exists to use as the verification baseline."
            )

        analysis_repo = ScanAnalysisRepository(self._session)

        # Filter for eligible occurrences: STANDARD scan, terminal status,
        # COMPLETED analysis.  Select the latest eligible one.
        eligible: OpportunityOccurrence | None = None
        for occ in occurrences:  # already ordered by created_at DESC
            scan = self._session.get(Scan, occ.scan_id)
            if scan is None:
                continue
            if scan.scan_type != ScanType.STANDARD:
                continue
            if scan.status not in (ScanStatus.COMPLETED, ScanStatus.PARTIAL):
                continue
            analysis = analysis_repo.get_by_scan_and_version(occ.scan_id, "deterministic-entity-v1")
            if analysis is None or analysis.status != ScanAnalysisStatus.COMPLETED:
                continue
            eligible = occ
            break

        if eligible is None:
            raise ValidationError(
                "Cannot transition to IMPLEMENTED: no eligible "
                "OpportunityOccurrence exists (STANDARD scan, terminal "
                "status, completed analysis)."
            )

        opp.implementation_baseline_occurrence_id = eligible.id
