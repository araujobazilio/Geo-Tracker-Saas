"""Phase 9 Competitor Explanation API endpoints.

All endpoints enforce tenant isolation. Cross-tenant access returns 404.

Role matrix:
- OWNER/ADMIN: read explanations
- MEMBER: read explanations
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.enums import LLMProvider, PromptType
from app.dependencies import (
    get_entitlement_service,
    get_workspace_auth_service,
    require_authenticated_user,
)
from app.models.user import User
from app.schemas.opportunity import (
    CompetitorExplanationResponse,
    CompetitorSummaryListResponse,
    CompetitorSummaryResponse,
    OverlapMatrixResponse,
    OwnedCitationEvidenceResponse,
    PromptGapEvidenceResponse,
    ProviderExplanationResponse,
    ReliabilityContextResponse,
)
from app.services.competitor_explanation_service import (
    CompetitorExplanationService,
    ReliabilityContext,
)
from app.services.entitlement_service import EntitlementService
from app.services.workspace_auth_service import WorkspaceAuthorizationService

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans",
    tags=["competitor-explanation"],
)


def get_explanation_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> CompetitorExplanationService:
    return CompetitorExplanationService(entitlement_service._session)


def _reliability_to_response(
    ctx: ReliabilityContext | None,
) -> ReliabilityContextResponse | None:
    if ctx is None:
        return None
    return ReliabilityContextResponse(
        confidence_scan_id=ctx.confidence_scan_id,
        overall_visibility_rate=ctx.overall_visibility_rate,
        mention_stability=ctx.mention_stability,
        repeat_sufficiency=ctx.repeat_sufficiency,
        observed_visibility_min=ctx.observed_visibility_min,
        observed_visibility_max=ctx.observed_visibility_max,
        confidence_level=ctx.confidence_level,
        confidence_methodology_version=ctx.confidence_methodology_version,
    )


@router.get(
    "/{scan_id}/competitors",
    response_model=CompetitorSummaryListResponse,
    status_code=status.HTTP_200_OK,
)
def list_competitor_summaries(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[CompetitorExplanationService, Depends(get_explanation_service)],
    prompt_type: Annotated[PromptType, Query()] = PromptType.NON_BRANDED,
) -> CompetitorSummaryListResponse:
    """List evidence-based summaries for all competitors in a scan."""
    auth.require_membership(workspace_id, user.id)
    summaries = service.list_competitor_summaries(workspace_id, project_id, scan_id, prompt_type)
    return CompetitorSummaryListResponse(
        scan_id=scan_id,
        competitors=[
            CompetitorSummaryResponse(
                entity_snapshot_id=s.entity_snapshot_id,
                entity_key=s.entity_key,
                name=s.name,
                domain=s.domain,
                brand_visibility_rate=s.brand_visibility_rate,
                competitor_visibility_rate=s.competitor_visibility_rate,
                visibility_gap_pp=s.visibility_gap_pp,
                brand_owned_citation_rate=s.brand_owned_citation_rate,
                competitor_owned_citation_rate=s.competitor_owned_citation_rate,
                citation_gap_pp=s.citation_gap_pp,
                competitor_only_runs=s.competitor_only_runs,
                reliability_context=_reliability_to_response(s.reliability_context),
            )
            for s in summaries
        ],
    )


@router.get(
    "/{scan_id}/competitors/{entity_snapshot_id}/explanation",
    response_model=CompetitorExplanationResponse,
    status_code=status.HTTP_200_OK,
)
def get_competitor_explanation(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    entity_snapshot_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[CompetitorExplanationService, Depends(get_explanation_service)],
    prompt_type: Annotated[PromptType, Query()] = PromptType.NON_BRANDED,
    provider: Annotated[LLMProvider | None, Query()] = None,
) -> CompetitorExplanationResponse:
    """Get detailed evidence-based explanation for a specific competitor."""
    auth.require_membership(workspace_id, user.id)
    explanation = service.get_explanation(
        workspace_id, project_id, scan_id, entity_snapshot_id, prompt_type, provider
    )
    return CompetitorExplanationResponse(
        scan_id=explanation.scan_id,
        competitor_entity_snapshot_id=explanation.competitor_entity_snapshot_id,
        competitor_entity_key=explanation.competitor_entity_key,
        competitor_name=explanation.competitor_name,
        competitor_domain=explanation.competitor_domain,
        brand_entity_snapshot_id=explanation.brand_entity_snapshot_id,
        brand_name=explanation.brand_name,
        brand_domain=explanation.brand_domain,
        prompt_type=explanation.prompt_type,
        provider_filter=explanation.provider_filter,
        brand_visibility_rate=explanation.brand_visibility_rate,
        competitor_visibility_rate=explanation.competitor_visibility_rate,
        visibility_gap_pp=explanation.visibility_gap_pp,
        brand_share_of_voice=explanation.brand_share_of_voice,
        competitor_share_of_voice=explanation.competitor_share_of_voice,
        brand_owned_citation_rate=explanation.brand_owned_citation_rate,
        competitor_owned_citation_rate=explanation.competitor_owned_citation_rate,
        citation_gap_pp=explanation.citation_gap_pp,
        successful_observations=explanation.successful_observations,
        measurement_coverage=explanation.measurement_coverage,
        overlap=OverlapMatrixResponse(
            brand_only_runs=explanation.overlap.brand_only_runs,
            competitor_only_runs=explanation.overlap.competitor_only_runs,
            both_runs=explanation.overlap.both_runs,
            neither_runs=explanation.overlap.neither_runs,
            successful_observations=explanation.overlap.successful_observations,
            competitor_only_rate=explanation.overlap.competitor_only_rate,
        ),
        provider_breakdown=[
            ProviderExplanationResponse(
                provider=pb.provider,
                planned_observations=pb.planned_observations,
                successful_observations=pb.successful_observations,
                measurement_coverage=pb.measurement_coverage,
                brand_visibility_rate=pb.brand_visibility_rate,
                competitor_visibility_rate=pb.competitor_visibility_rate,
                visibility_gap_pp=pb.visibility_gap_pp,
                brand_owned_citation_rate=pb.brand_owned_citation_rate,
                competitor_owned_citation_rate=pb.competitor_owned_citation_rate,
                competitor_only_runs=pb.competitor_only_runs,
            )
            for pb in explanation.provider_breakdown
        ],
        prompt_gaps=[
            PromptGapEvidenceResponse(
                prompt_id=pg.prompt_id,
                prompt_text=pg.prompt_text,
                prompt_type=pg.prompt_type,
                intent=pg.intent,
                funnel_stage=pg.funnel_stage,
                commercial_intent=pg.commercial_intent,
                affected_providers=pg.affected_providers,
                successful_observations=pg.successful_observations,
                competitor_only_count=pg.competitor_only_count,
            )
            for pg in explanation.prompt_gaps
        ],
        owned_citation_evidence=[
            OwnedCitationEvidenceResponse(
                response_source_id=src.response_source_id,
                url=src.url,
                title=src.title,
                provider=src.provider,
                prompt_run_id=src.prompt_run_id,
                prompt_id=src.prompt_id,
            )
            for src in explanation.owned_citation_evidence
        ],
        reliability_context=_reliability_to_response(explanation.reliability_context),
    )
