"""Phase 9 Action Center API endpoints.

All endpoints enforce tenant isolation. Cross-tenant access returns 404.

Role matrix:
- OWNER/ADMIN: read + refresh + update status
- MEMBER: read only
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.core.enums import (
    LLMProvider,
    OpportunityPriority,
    OpportunityStatus,
    OpportunityType,
    WorkspaceRole,
)
from app.dependencies import (
    get_entitlement_service,
    get_workspace_auth_service,
    require_authenticated_user,
)
from app.models.opportunity import Opportunity, OpportunityEvidence, OpportunityOccurrence
from app.models.user import User
from app.schemas.opportunity import (
    OpportunityDetailResponse,
    OpportunityEvidenceResponse,
    OpportunityListResponse,
    OpportunityOccurrenceResponse,
    OpportunityResponse,
    OpportunityStatusUpdateRequest,
    RefreshActionsResponse,
)
from app.services.action_generation_service import ActionGenerationService
from app.services.entitlement_service import EntitlementService
from app.services.opportunity_workflow_service import OpportunityWorkflowService
from app.services.workspace_auth_service import WorkspaceAuthorizationService

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/projects/{project_id}",
    tags=["action-center"],
)

# Also register scan-scoped refresh endpoint under scans prefix.
scan_router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans",
    tags=["action-center"],
)


def get_action_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> ActionGenerationService:
    return ActionGenerationService(entitlement_service._session)


def get_workflow_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> OpportunityWorkflowService:
    return OpportunityWorkflowService(entitlement_service._session)


# Status sort priority for default ordering.
_STATUS_ORDER = {
    OpportunityStatus.IN_PROGRESS: 0,
    OpportunityStatus.OPEN: 1,
    OpportunityStatus.IMPLEMENTED: 2,
    OpportunityStatus.DISMISSED: 3,
    OpportunityStatus.VERIFIED: 4,
}
_PRIORITY_ORDER = {
    OpportunityPriority.HIGH: 0,
    OpportunityPriority.MEDIUM: 1,
    OpportunityPriority.LOW: 2,
}


@router.get(
    "/opportunities",
    response_model=OpportunityListResponse,
    status_code=status.HTTP_200_OK,
)
def list_opportunities(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    status_filter: Annotated[OpportunityStatus | None, Query(alias="status")] = None,
    priority_filter: Annotated[OpportunityPriority | None, Query(alias="priority")] = None,
    opportunity_type_filter: Annotated[
        OpportunityType | None, Query(alias="opportunity_type")
    ] = None,
    provider_filter: Annotated[LLMProvider | None, Query(alias="provider")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> OpportunityListResponse:
    """List Action Center opportunities with filters and deterministic sorting."""
    auth.require_membership(workspace_id, user.id)
    session = entitlement_service._session

    query = select(Opportunity).where(
        Opportunity.workspace_id == workspace_id,
        Opportunity.project_id == project_id,
    )
    if status_filter is not None:
        query = query.where(Opportunity.status == status_filter)
    if priority_filter is not None:
        query = query.where(Opportunity.priority == priority_filter)
    if opportunity_type_filter is not None:
        query = query.where(Opportunity.opportunity_type == opportunity_type_filter)
    if provider_filter is not None:
        query = query.where(Opportunity.provider == provider_filter)

    # Count total.
    from sqlalchemy import func

    count_query = select(func.count()).select_from(query.subquery())
    total = session.execute(count_query).scalar_one()

    # Sort: status relevance, priority HIGH→LOW, last_detected_at DESC.
    all_opps = list(session.execute(query).scalars())
    all_opps.sort(
        key=lambda o: (
            _STATUS_ORDER.get(o.status, 99),
            _PRIORITY_ORDER.get(o.priority, 99),
            -(o.last_detected_at.timestamp() if o.last_detected_at else 0),
        )
    )
    page = all_opps[offset : offset + limit]

    return OpportunityListResponse(
        items=[OpportunityResponse.model_validate(o) for o in page],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/opportunities/{opportunity_id}",
    response_model=OpportunityDetailResponse,
    status_code=status.HTTP_200_OK,
)
def get_opportunity(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> OpportunityDetailResponse:
    """Get detailed opportunity with latest occurrence and evidence."""
    auth.require_membership(workspace_id, user.id)
    session = entitlement_service._session

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

    # Load occurrences ordered by created_at desc.
    occurrences = list(
        session.execute(
            select(OpportunityOccurrence)
            .where(OpportunityOccurrence.opportunity_id == opp.id)
            .order_by(OpportunityOccurrence.created_at.desc())
        ).scalars()
    )

    latest_occ = None
    if occurrences:
        latest = occurrences[0]
        evidence = list(
            session.execute(
                select(OpportunityEvidence).where(OpportunityEvidence.occurrence_id == latest.id)
            ).scalars()
        )
        latest_occ = OpportunityOccurrenceResponse(
            id=latest.id,
            scan_id=latest.scan_id,
            scan_analysis_id=latest.scan_analysis_id,
            priority_at_detection=latest.priority_at_detection,
            action_engine_version_at_detection=latest.action_engine_version_at_detection,
            brand_visibility=latest.brand_visibility,
            competitor_visibility=latest.competitor_visibility,
            visibility_gap_pp=latest.visibility_gap_pp,
            brand_citation_rate=latest.brand_citation_rate,
            competitor_citation_rate=latest.competitor_citation_rate,
            citation_gap_pp=latest.citation_gap_pp,
            measurement_coverage=latest.measurement_coverage,
            created_at=latest.created_at,
            evidence=[OpportunityEvidenceResponse.model_validate(e) for e in evidence],
        )

    return OpportunityDetailResponse(
        **OpportunityResponse.model_validate(opp).model_dump(),
        latest_occurrence=latest_occ,
        occurrence_count=len(occurrences),
        reliability_context=None,
    )


@scan_router.post(
    "/{scan_id}/actions/refresh",
    response_model=RefreshActionsResponse,
    status_code=status.HTTP_200_OK,
)
def refresh_actions(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[ActionGenerationService, Depends(get_action_service)],
) -> RefreshActionsResponse:
    """Refresh Action Center from a STANDARD scan's evidence.

    Performs only deterministic local computation. 0 AI Checks, 0 provider calls.
    OWNER/ADMIN only.
    """
    auth.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    result = service.refresh_from_scan(workspace_id, project_id, scan_id)
    return RefreshActionsResponse(
        action_engine_version=result.action_engine_version,
        scan_id=result.scan_id,
        opportunities_detected=result.opportunities_detected,
        opportunities_created=result.opportunities_created,
        opportunities_updated=result.opportunities_updated,
        occurrences_created=result.occurrences_created,
        warnings=result.warnings,
    )


@router.patch(
    "/opportunities/{opportunity_id}",
    response_model=OpportunityResponse,
    status_code=status.HTTP_200_OK,
)
def update_opportunity_status(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    body: OpportunityStatusUpdateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[OpportunityWorkflowService, Depends(get_workflow_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> OpportunityResponse:
    """Update opportunity workflow status. OWNER/ADMIN only."""
    auth.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    service.transition(workspace_id, project_id, opportunity_id, body.status, body.dismissal_reason)
    entitlement_service._session.flush()
    opp = entitlement_service._session.execute(
        select(Opportunity).where(Opportunity.id == opportunity_id)
    ).scalar_one()
    return OpportunityResponse.model_validate(opp)
