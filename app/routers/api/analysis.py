"""Phase 7 analysis and metrics API endpoints.

All endpoints enforce tenant isolation (workspace → project → scan/run
hierarchy). Cross-tenant access returns 404.

Role matrix:
- OWNER/ADMIN: read analysis, trigger deterministic retry
- MEMBER: read only
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.enums import LLMProvider, PromptType, WorkspaceRole
from app.dependencies import (
    get_entitlement_service,
    get_workspace_auth_service,
    require_authenticated_user,
)
from app.models.user import User
from app.schemas.analysis import (
    AnalysisResponse,
    EntityMentionResponse,
    EntitySnapshotResponse,
    MetricsResponse,
    RunAnalysisResponse,
    SourceAttributionResponse,
)
from app.services.entitlement_service import EntitlementService
from app.services.scan_analysis_service import ScanAnalysisService
from app.services.visibility_metrics_service import VisibilityMetricsService
from app.services.workspace_auth_service import WorkspaceAuthorizationService

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans",
    tags=["analysis"],
)


def get_analysis_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> ScanAnalysisService:
    return ScanAnalysisService(entitlement_service._session)


def get_metrics_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> VisibilityMetricsService:
    return VisibilityMetricsService(entitlement_service._session)


@router.post(
    "/{scan_id}/analysis",
    response_model=AnalysisResponse,
    status_code=status.HTTP_200_OK,
)
def trigger_analysis(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[ScanAnalysisService, Depends(get_analysis_service)],
) -> AnalysisResponse:
    """Run or retry LOCAL deterministic analysis.

    Consumes 0 AI Checks and makes 0 provider calls.
    OWNER/ADMIN only.
    """
    auth.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    analysis = service.analyze(scan_id)
    from app.repositories.analysis_repository import ScanEntitySnapshotRepository

    snapshots = ScanEntitySnapshotRepository(service._session).list_by_scan(scan_id)
    return AnalysisResponse(
        id=analysis.id,
        scan_id=analysis.scan_id,
        analysis_version=analysis.analysis_version,
        status=analysis.status,
        started_at=analysis.started_at,
        completed_at=analysis.completed_at,
        failure_code=analysis.failure_code,
        failure_message=analysis.failure_message,
        warning_count=analysis.warning_count,
        created_at=analysis.created_at,
        entity_snapshots=[EntitySnapshotResponse.model_validate(s) for s in snapshots],
    )


@router.get("/{scan_id}/analysis", response_model=AnalysisResponse)
def get_analysis(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[ScanAnalysisService, Depends(get_analysis_service)],
) -> AnalysisResponse:
    """Get the current analysis for a scan."""
    auth.require_membership(workspace_id, user.id)
    from app.core.exceptions import NotFoundError
    from app.repositories.analysis_repository import ScanEntitySnapshotRepository

    analysis = service.get_analysis(scan_id)
    if analysis is None:
        raise NotFoundError("Analysis not found for this scan.")
    snapshots = ScanEntitySnapshotRepository(service._session).list_by_scan(scan_id)
    return AnalysisResponse(
        id=analysis.id,
        scan_id=analysis.scan_id,
        analysis_version=analysis.analysis_version,
        status=analysis.status,
        started_at=analysis.started_at,
        completed_at=analysis.completed_at,
        failure_code=analysis.failure_code,
        failure_message=analysis.failure_message,
        warning_count=analysis.warning_count,
        created_at=analysis.created_at,
        entity_snapshots=[EntitySnapshotResponse.model_validate(s) for s in snapshots],
    )


@router.get("/{scan_id}/metrics", response_model=MetricsResponse)
def get_metrics(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[VisibilityMetricsService, Depends(get_metrics_service)],
    prompt_type: Annotated[PromptType, Query()] = PromptType.NON_BRANDED,
    provider: Annotated[LLMProvider | None, Query()] = None,
) -> MetricsResponse:
    """Get visibility metrics for a scan."""
    auth.require_membership(workspace_id, user.id)
    result = service.get_metrics(workspace_id, project_id, scan_id, prompt_type, provider)
    from app.schemas.analysis import (
        EntityMetricResponse,
        ProviderBreakdownResponse,
    )

    return MetricsResponse(
        scan_id=result.scan_id,
        scan_status=result.scan_status,
        analysis_version=result.analysis_version,
        analysis_status=result.analysis_status,
        prompt_set_version=result.prompt_set_version,
        scope=result.scope,
        provider_filter=result.provider_filter,
        planned_observations=result.planned_observations,
        successful_observations=result.successful_observations,
        measurement_coverage=result.measurement_coverage,
        entity_metrics=[
            EntityMetricResponse(
                entity_snapshot_id=em.entity_snapshot_id,
                entity_type=em.entity_type,
                name=em.name,
                domain=em.domain,
                planned_observations=em.planned_observations,
                successful_observations=em.successful_observations,
                mentioned_observations=em.mentioned_observations,
                visibility_rate=em.visibility_rate,
                share_of_voice=em.share_of_voice,
                citation_eligible_observations=em.citation_eligible_observations,
                owned_cited_observations=em.owned_cited_observations,
                owned_source_count=em.owned_source_count,
                owned_citation_rate=em.owned_citation_rate,
                owned_source_share=em.owned_source_share,
            )
            for em in result.entity_metrics
        ],
        provider_breakdown=[
            ProviderBreakdownResponse(
                provider=pb.provider,
                successful_observations=pb.successful_observations,
                planned_observations=pb.planned_observations,
                measurement_coverage=pb.measurement_coverage,
                visibility_rate=pb.visibility_rate,
                citation_eligible_observations=pb.citation_eligible_observations,
                owned_citation_rate=pb.owned_citation_rate,
            )
            for pb in result.provider_breakdown
        ],
        leaderboard=[
            EntityMetricResponse(
                entity_snapshot_id=em.entity_snapshot_id,
                entity_type=em.entity_type,
                name=em.name,
                domain=em.domain,
                planned_observations=em.planned_observations,
                successful_observations=em.successful_observations,
                mentioned_observations=em.mentioned_observations,
                visibility_rate=em.visibility_rate,
                share_of_voice=em.share_of_voice,
                citation_eligible_observations=em.citation_eligible_observations,
                owned_cited_observations=em.owned_cited_observations,
                owned_source_count=em.owned_source_count,
                owned_citation_rate=em.owned_citation_rate,
                owned_source_share=em.owned_source_share,
            )
            for em in result.leaderboard
        ],
    )


@router.get("/{scan_id}/runs/{prompt_run_id}/analysis", response_model=RunAnalysisResponse)
def get_run_analysis(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    prompt_run_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[ScanAnalysisService, Depends(get_analysis_service)],
) -> RunAnalysisResponse:
    """Get mention and attribution evidence for a specific prompt run."""
    auth.require_membership(workspace_id, user.id)
    from app.core.exceptions import NotFoundError
    from app.repositories.analysis_repository import (
        EntityMentionRepository,
        SourceAttributionRepository,
    )
    from app.repositories.scan_repository import PromptRunRepository

    # Verify the run belongs to this scan and workspace.
    run_repo = PromptRunRepository(service._session)
    run = run_repo.get_scoped(workspace_id, project_id, scan_id, prompt_run_id)
    if run is None:
        raise NotFoundError("PromptRun not found.")

    analysis = service.get_analysis(scan_id)
    if analysis is None:
        raise NotFoundError("Analysis not found for this scan.")

    mention_repo = EntityMentionRepository(service._session)
    attr_repo = SourceAttributionRepository(service._session)

    mentions = mention_repo.list_by_analysis_and_run(analysis.id, prompt_run_id)
    # Attributions are per source, not per run. We need to find attributions
    # whose response_source belongs to this run.
    from sqlalchemy import select

    from app.models.scan import ResponseSource

    source_ids = {
        s.id
        for s in service._session.execute(
            select(ResponseSource).where(ResponseSource.prompt_run_id == prompt_run_id)
        ).scalars()
    }
    all_attrs = attr_repo.list_by_analysis(analysis.id)
    run_attrs = [a for a in all_attrs if a.response_source_id in source_ids]

    return RunAnalysisResponse(
        prompt_run_id=prompt_run_id,
        mentions=[EntityMentionResponse.model_validate(m) for m in mentions],
        attributions=[SourceAttributionResponse.model_validate(a) for a in run_attrs],
    )
