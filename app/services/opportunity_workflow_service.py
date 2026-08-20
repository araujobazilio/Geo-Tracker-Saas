"""OpportunityWorkflowService — status transition management.

Enforces the Phase 9 allowed status transitions for Opportunities.
VERIFIED is read-only in Phase 9 (reserved for Phase 10 Verification Scans).

Allowed transitions:
- OPEN → IN_PROGRESS, DISMISSED
- IN_PROGRESS → OPEN, IMPLEMENTED, DISMISSED
- IMPLEMENTED → IN_PROGRESS, DISMISSED
- DISMISSED → OPEN

Forbidden:
- Any → VERIFIED (Phase 9 cannot verify)
- VERIFIED → anything (read-only)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import OpportunityStatus
from app.core.exceptions import NotFoundError, ValidationError

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
    OpportunityStatus.VERIFIED: set(),  # read-only in Phase 9
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
        from app.models.opportunity import Opportunity

        opp = self._session.execute(
            select(Opportunity).where(
                Opportunity.id == opportunity_id,
                Opportunity.workspace_id == workspace_id,
                Opportunity.project_id == project_id,
            )
        ).scalar_one_or_none()

        if opp is None:
            raise NotFoundError("Opportunity not found.")

        current = opp.status
        if current == new_status:
            return current  # no-op

        if new_status == OpportunityStatus.VERIFIED:
            raise ValidationError("VERIFIED status is reserved for Phase 10 Verification Scans.")

        allowed = _ALLOWED_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise ValidationError(
                f"Transition from {current.value} to {new_status.value} is not allowed."
            )

        now = datetime.now(UTC)
        opp.status = new_status

        if new_status == OpportunityStatus.IMPLEMENTED:
            opp.implemented_at = now
        elif new_status == OpportunityStatus.DISMISSED:
            opp.dismissed_at = now
            if dismissal_reason:
                opp.dismissal_reason = dismissal_reason
        elif new_status == OpportunityStatus.OPEN:
            # Reopening: clear dismissed/implemented timestamps.
            opp.dismissed_at = None
            opp.dismissal_reason = None
            if current == OpportunityStatus.IMPLEMENTED:
                opp.implemented_at = None
        elif new_status == OpportunityStatus.IN_PROGRESS and current == OpportunityStatus.DISMISSED:
            # Starting work: clear dismissed if reopening from dismissed.
            opp.dismissed_at = None
            opp.dismissal_reason = None

        self._session.commit()
        return new_status
