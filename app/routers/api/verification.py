"""Phase 10 Verification Scans API endpoints.

POST   /api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}/verification
GET    /api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}/verifications
GET    /api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}/verifications/{verification_id}
POST   /api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}/verifications/{verification_id}/evaluate
GET    /api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}/verification-summary

Role matrix:
- OWNER/ADMIN: create verification scan + trigger evaluation
- MEMBER: read only

All endpoints enforce tenant isolation. Cross-tenant access returns 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import WorkspaceRole
from app.dependencies import (
    get_entitlement_service,
    get_workspace_auth_service,
    require_authenticated_user,
)
from app.models.opportunity import Opportunity, OpportunityVerification
from app.models.user import User
from app.schemas.verification import (
    VerificationEvaluationResponse,
    VerificationListResponse,
    VerificationResponse,
    VerificationScanCreateRequest,
    VerificationScanCreateResponse,
    VerificationSummaryResponse,
)
from app.services.entitlement_service import EntitlementService
from app.services.verification_evaluation_service import VerificationEvaluationService
from app.services.verification_scan_creation_service import (
    VerificationScanCreationResult,
    VerificationScanCreationService,
)
from app.services.workspace_auth_service import WorkspaceAuthorizationService

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities",
    tags=["verification"],
)


def get_verification_scan_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> VerificationScanCreationService:
    from app.services.scanning.dispatcher import CeleryScanDispatcher

    return VerificationScanCreationService(
        entitlement_service._session,
        CeleryScanDispatcher(),
    )


def get_verification_evaluation_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> VerificationEvaluationService:
    return VerificationEvaluationService(entitlement_service._session)


def _load_opportunity(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    session: Session,
) -> Opportunity:
    opp = session.execute(
        select(Opportunity).where(
            Opportunity.id == opportunity_id,
            Opportunity.workspace_id == workspace_id,
            Opportunity.project_id == project_id,
        )
    ).scalar_one_or_none()
    if opp is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("Opportunity not found.")
    return opp


@router.post(
    "/{opportunity_id}/verification",
    response_model=VerificationScanCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_verification_scan(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    _body: VerificationScanCreateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[VerificationScanCreationService, Depends(get_verification_scan_service)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> VerificationScanCreateResponse:
    """Create a VERIFICATION scan for an IMPLEMENTED Opportunity.

    Clones the frozen implementation baseline STANDARD scan's exact
    methodology (prompts, providers, surfaces, modes, models, entity
    snapshots) and dispatches a new VERIFICATION scan.  0 AI Checks
    charged at creation; quota is reserved for prompt_count x
    provider_count checks.

    OWNER/ADMIN only. Requires verification_scans entitlement.
    """
    auth.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    result: VerificationScanCreationResult = service.create_verification_scan(
        workspace_id=workspace_id,
        project_id=project_id,
        opportunity_id=opportunity_id,
        requested_by_user_id=user.id,
        idempotency_key=idempotency_key,
    )
    scan = result.scan
    verification = result.verification
    return VerificationScanCreateResponse(
        scan_id=scan.id,
        verification_id=verification.id,
        opportunity_id=verification.opportunity_id,
        baseline_scan_id=verification.baseline_scan_id,
        baseline_occurrence_id=verification.baseline_occurrence_id,
        scan_type=scan.scan_type,
        status=scan.status,
        prompt_count=scan.prompt_count,
        provider_count=scan.provider_count,
        planned_ai_checks=scan.planned_ai_checks,
        verification_methodology_version=verification.verification_methodology_version,
    )


@router.get(
    "/{opportunity_id}/verifications",
    response_model=VerificationListResponse,
    status_code=status.HTTP_200_OK,
)
def list_verifications(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> VerificationListResponse:
    """List verification records for an Opportunity, newest first."""
    auth.require_membership(workspace_id, user.id)
    session = entitlement_service._session
    _load_opportunity(workspace_id, project_id, opportunity_id, session)

    base_query = select(OpportunityVerification).where(
        OpportunityVerification.workspace_id == workspace_id,
        OpportunityVerification.project_id == project_id,
        OpportunityVerification.opportunity_id == opportunity_id,
    )
    total = session.execute(select(func.count()).select_from(base_query.subquery())).scalar_one()

    rows = list(
        session.execute(
            base_query.order_by(OpportunityVerification.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).scalars()
    )
    return VerificationListResponse(
        items=[VerificationResponse.model_validate(v) for v in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{opportunity_id}/verifications/{verification_id}",
    response_model=VerificationResponse,
    status_code=status.HTTP_200_OK,
)
def get_verification(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    verification_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> VerificationResponse:
    """Get a single verification record by ID."""
    auth.require_membership(workspace_id, user.id)
    session = entitlement_service._session
    verification = session.execute(
        select(OpportunityVerification).where(
            OpportunityVerification.id == verification_id,
            OpportunityVerification.workspace_id == workspace_id,
            OpportunityVerification.project_id == project_id,
            OpportunityVerification.opportunity_id == opportunity_id,
        )
    ).scalar_one_or_none()
    if verification is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("Verification not found.")
    return VerificationResponse.model_validate(verification)


@router.post(
    "/{opportunity_id}/verifications/{verification_id}/evaluate",
    response_model=VerificationEvaluationResponse,
    status_code=status.HTTP_200_OK,
)
def evaluate_verification(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    verification_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[VerificationEvaluationService, Depends(get_verification_evaluation_service)],
) -> VerificationEvaluationResponse:
    """Trigger deterministic evaluation of a verification comparison.

    Computes the before/after metric comparison and persists the
    VerificationOutcome.  If the outcome is RESOLVED, transitions the
    parent Opportunity to VERIFIED.

    OWNER/ADMIN only.  Zero AI Checks, zero provider calls.
    """
    auth.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    result = service.evaluate(verification_id)
    return VerificationEvaluationResponse(
        verification_id=result.verification_id,
        opportunity_id=result.opportunity_id,
        outcome=result.outcome,
        reason_code=result.reason_code,
        evaluation_message=result.evaluation_message,
        metric_name=result.metric_name,
        baseline_value=result.baseline_value,
        verification_value=result.verification_value,
        delta_value=result.delta_value,
        baseline_brand_value=result.baseline_brand_value,
        verification_brand_value=result.verification_brand_value,
        baseline_coverage=result.baseline_coverage,
        verification_coverage=result.verification_coverage,
        resolution_threshold=result.resolution_threshold,
        meaningful_improvement_threshold=result.meaningful_improvement_threshold,
        opportunity_status_after=result.opportunity_status_after,
    )


@router.get(
    "/{opportunity_id}/verification-summary",
    response_model=VerificationSummaryResponse,
    status_code=status.HTTP_200_OK,
)
def get_verification_summary(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> VerificationSummaryResponse:
    """Get a summary of verification outcomes for an Opportunity."""
    auth.require_membership(workspace_id, user.id)
    session = entitlement_service._session
    opp = _load_opportunity(workspace_id, project_id, opportunity_id, session)

    rows = list(
        session.execute(
            select(OpportunityVerification)
            .where(
                OpportunityVerification.workspace_id == workspace_id,
                OpportunityVerification.project_id == project_id,
                OpportunityVerification.opportunity_id == opportunity_id,
            )
            .order_by(OpportunityVerification.created_at.desc())
        ).scalars()
    )

    counts = {
        "resolved": 0,
        "improved": 0,
        "not_improved": 0,
        "regressed": 0,
        "inconclusive": 0,
        "pending": 0,
    }
    latest_outcome = None
    latest_evaluated_at = None
    for v in rows:
        outcome = v.outcome.value if hasattr(v.outcome, "value") else str(v.outcome)
        key = outcome.lower()
        if key in counts:
            counts[key] += 1
        if latest_outcome is None:
            latest_outcome = v.outcome
            latest_evaluated_at = v.evaluated_at

    return VerificationSummaryResponse(
        opportunity_id=opportunity_id,
        total_verifications=len(rows),
        resolved_count=counts["resolved"],
        improved_count=counts["improved"],
        not_improved_count=counts["not_improved"],
        regressed_count=counts["regressed"],
        inconclusive_count=counts["inconclusive"],
        pending_count=counts["pending"],
        latest_outcome=latest_outcome,
        latest_evaluated_at=latest_evaluated_at,
        opportunity_status=opp.status,
    )
