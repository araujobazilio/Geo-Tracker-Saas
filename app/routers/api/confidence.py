"""Confidence Scan API endpoints (Phase 8).

POST   /api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{baseline_scan_id}/confidence
GET    /api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}/confidence

Role matrix:
- OWNER/ADMIN: create + read
- MEMBER: read only
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status

from app.core.enums import LLMProvider, PromptType, WorkspaceRole
from app.dependencies import (
    get_audit_service,
    get_entitlement_service,
    get_workspace_auth_service,
    require_authenticated_user,
)
from app.models.user import User
from app.schemas.confidence import (
    ConfidenceMetricsResponse,
    ConfidenceScanCreateRequest,
    ConfidenceScanCreateResponse,
    EntityReliabilityResponse,
    ProviderReliabilityResponse,
    RoundSummaryResponse,
)
from app.services.audit_service import AuditService
from app.services.confidence_metrics_service import ConfidenceMetricsService
from app.services.confidence_scan_creation_service import ConfidenceScanCreationService
from app.services.entitlement_service import EntitlementService
from app.services.scanning.dispatcher import CeleryScanDispatcher, ScanDispatcher
from app.services.workspace_auth_service import WorkspaceAuthorizationService

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans",
    tags=["confidence"],
)


def get_scan_dispatcher() -> ScanDispatcher:
    return CeleryScanDispatcher()


def get_confidence_creation_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    dispatcher: Annotated[ScanDispatcher, Depends(get_scan_dispatcher)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> ConfidenceScanCreationService:
    return ConfidenceScanCreationService(
        entitlement_service._session,
        dispatcher,
        audit_service=audit,
    )


def get_confidence_metrics_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> ConfidenceMetricsService:
    return ConfidenceMetricsService(entitlement_service._session)


@router.post(
    "/{baseline_scan_id}/confidence",
    response_model=ConfidenceScanCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_confidence_scan(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    baseline_scan_id: uuid.UUID,
    request: ConfidenceScanCreateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[ConfidenceScanCreationService, Depends(get_confidence_creation_service)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ConfidenceScanCreateResponse:
    """Create a Confidence Scan from a baseline STANDARD scan.

    Repeats the baseline's exact Prompt x Provider measurement cells
    repeat_count times. All methodology is cloned from the baseline;
    no custom prompts, providers, or models are accepted.

    Requires ADMIN role and confidence_scans entitlement.
    """
    auth.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    result = service.create_confidence_scan(
        workspace_id=workspace_id,
        project_id=project_id,
        baseline_scan_id=baseline_scan_id,
        requested_by_user_id=user.id,
        idempotency_key=idempotency_key,
        repeat_count=request.repeat_count,
    )
    scan = result.scan
    return ConfidenceScanCreateResponse(
        scan_id=scan.id,
        baseline_scan_id=scan.baseline_scan_id,  # type: ignore[arg-type]
        scan_type=scan.scan_type,
        repeat_count=scan.repeat_count,
        prompt_count=scan.prompt_count,
        provider_count=scan.provider_count,
        planned_ai_checks=scan.planned_ai_checks,
        status=scan.status,
    )


@router.get(
    "/{scan_id}/confidence",
    response_model=ConfidenceMetricsResponse,
)
def get_confidence_metrics(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[ConfidenceMetricsService, Depends(get_confidence_metrics_service)],
    prompt_type: Annotated[PromptType, Query()] = PromptType.NON_BRANDED,
    provider: Annotated[LLMProvider | None, Query()] = None,
) -> ConfidenceMetricsResponse:
    """Get confidence/reliability metrics for a CONFIDENCE scan.

    Returns measurement coverage, round summaries, entity reliability,
    provider breakdown, and the overall confidence level.

    This is a product evidence-quality label, NOT a statistical
    confidence interval.
    """
    auth.require_membership(workspace_id, user.id)
    result = service.get_metrics(
        workspace_id=workspace_id,
        project_id=project_id,
        scan_id=scan_id,
        prompt_type=prompt_type,
        provider=provider,
    )
    return ConfidenceMetricsResponse(
        scan_id=result.scan_id,
        baseline_scan_id=result.baseline_scan_id,
        repeat_count=result.repeat_count,
        confidence_methodology_version=result.confidence_methodology_version,
        scope=result.scope,
        provider_filter=result.provider_filter,
        planned_observations=result.planned_observations,
        successful_observations=result.successful_observations,
        measurement_coverage=result.measurement_coverage,
        overall_confidence_level=result.overall_confidence_level,
        round_summaries=[
            RoundSummaryResponse(
                observation_index=rs.observation_index,
                planned_observations=rs.planned_observations,
                successful_observations=rs.successful_observations,
                measurement_coverage=rs.measurement_coverage,
                entity_visibility={str(k): v for k, v in rs.entity_visibility.items()},
            )
            for rs in result.round_summaries
        ],
        entity_reliability=[
            EntityReliabilityResponse(
                entity_snapshot_id=er.entity_snapshot_id,
                entity_type=er.entity_type,
                name=er.name,
                domain=er.domain,
                overall_visibility_rate=er.overall_visibility_rate,
                planned_cells=er.planned_cells,
                repeat_analyzable_cells=er.repeat_analyzable_cells,
                stable_cells=er.stable_cells,
                variable_cells=er.variable_cells,
                insufficient_cells=er.insufficient_cells,
                repeat_sufficiency=er.repeat_sufficiency,
                mention_stability=er.mention_stability,
                observed_visibility_min=er.observed_visibility_min,
                observed_visibility_max=er.observed_visibility_max,
                observed_visibility_range=er.observed_visibility_range,
                confidence_level=er.confidence_level,
            )
            for er in result.entity_reliability
        ],
        provider_breakdown=[
            ProviderReliabilityResponse(
                provider=pr.provider,
                planned_observations=pr.planned_observations,
                successful_observations=pr.successful_observations,
                measurement_coverage=pr.measurement_coverage,
                brand_visibility_rate=pr.brand_visibility_rate,
                observed_visibility_min=pr.observed_visibility_min,
                observed_visibility_max=pr.observed_visibility_max,
                repeat_sufficiency=pr.repeat_sufficiency,
                mention_stability=pr.mention_stability,
                confidence_level=pr.confidence_level,
            )
            for pr in result.provider_breakdown
        ],
    )
