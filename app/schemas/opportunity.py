"""Phase 9 Action Center and Competitor Explanation API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.core.enums import (
    LLMProvider,
    OpportunityEvidenceType,
    OpportunityPriority,
    OpportunityStatus,
    OpportunityType,
    PromptType,
)

# --- Competitor Explanation schemas ---


class OverlapMatrixResponse(BaseModel):
    brand_only_runs: int
    competitor_only_runs: int
    both_runs: int
    neither_runs: int
    successful_observations: int
    competitor_only_rate: Decimal | None


class ProviderExplanationResponse(BaseModel):
    provider: LLMProvider
    planned_observations: int
    successful_observations: int
    measurement_coverage: Decimal | None
    brand_visibility_rate: Decimal | None
    competitor_visibility_rate: Decimal | None
    visibility_gap_pp: Decimal | None
    brand_owned_citation_rate: Decimal | None
    competitor_owned_citation_rate: Decimal | None
    competitor_only_runs: int


class PromptGapEvidenceResponse(BaseModel):
    prompt_id: uuid.UUID
    prompt_text: str
    prompt_type: PromptType
    intent: str | None
    funnel_stage: str | None
    commercial_intent: bool
    affected_providers: list[LLMProvider]
    successful_observations: int
    competitor_only_count: int


class OwnedCitationEvidenceResponse(BaseModel):
    response_source_id: uuid.UUID
    url: str
    title: str | None
    provider: LLMProvider
    prompt_run_id: uuid.UUID
    prompt_id: uuid.UUID


class ReliabilityContextResponse(BaseModel):
    confidence_scan_id: uuid.UUID
    overall_visibility_rate: Decimal | None
    mention_stability: Decimal | None
    repeat_sufficiency: Decimal | None
    observed_visibility_min: Decimal | None
    observed_visibility_max: Decimal | None
    confidence_level: str
    confidence_methodology_version: str


class CompetitorExplanationResponse(BaseModel):
    scan_id: uuid.UUID
    competitor_entity_snapshot_id: uuid.UUID
    competitor_entity_key: str
    competitor_name: str
    competitor_domain: str
    brand_entity_snapshot_id: uuid.UUID
    brand_name: str
    brand_domain: str
    prompt_type: PromptType
    provider_filter: LLMProvider | None
    brand_visibility_rate: Decimal | None
    competitor_visibility_rate: Decimal | None
    visibility_gap_pp: Decimal | None
    brand_share_of_voice: Decimal | None
    competitor_share_of_voice: Decimal | None
    brand_owned_citation_rate: Decimal | None
    competitor_owned_citation_rate: Decimal | None
    citation_gap_pp: Decimal | None
    successful_observations: int
    measurement_coverage: Decimal | None
    overlap: OverlapMatrixResponse
    provider_breakdown: list[ProviderExplanationResponse]
    prompt_gaps: list[PromptGapEvidenceResponse]
    owned_citation_evidence: list[OwnedCitationEvidenceResponse]
    reliability_context: ReliabilityContextResponse | None


class CompetitorSummaryResponse(BaseModel):
    entity_snapshot_id: uuid.UUID
    entity_key: str
    name: str
    domain: str
    brand_visibility_rate: Decimal | None
    competitor_visibility_rate: Decimal | None
    visibility_gap_pp: Decimal | None
    brand_owned_citation_rate: Decimal | None
    competitor_owned_citation_rate: Decimal | None
    citation_gap_pp: Decimal | None
    competitor_only_runs: int
    reliability_context: ReliabilityContextResponse | None


class CompetitorSummaryListResponse(BaseModel):
    scan_id: uuid.UUID
    competitors: list[CompetitorSummaryResponse]


# --- Opportunity schemas ---


class OpportunityEvidenceResponse(BaseModel):
    id: uuid.UUID
    evidence_key: str
    evidence_type: OpportunityEvidenceType
    prompt_id: uuid.UUID | None
    prompt_run_id: uuid.UUID | None
    response_source_id: uuid.UUID | None
    provider: LLMProvider | None
    metric_name: str | None
    brand_value: Decimal | None
    competitor_value: Decimal | None
    delta_value: Decimal | None

    model_config = {"from_attributes": True}


class OpportunityOccurrenceResponse(BaseModel):
    id: uuid.UUID
    scan_id: uuid.UUID
    scan_analysis_id: uuid.UUID
    priority_at_detection: OpportunityPriority
    brand_visibility: Decimal | None
    competitor_visibility: Decimal | None
    visibility_gap_pp: Decimal | None
    brand_citation_rate: Decimal | None
    competitor_citation_rate: Decimal | None
    citation_gap_pp: Decimal | None
    measurement_coverage: Decimal | None
    created_at: datetime
    evidence: list[OpportunityEvidenceResponse]

    model_config = {"from_attributes": True}


class OpportunityResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    fingerprint: str
    opportunity_type: OpportunityType
    status: OpportunityStatus
    priority: OpportunityPriority
    action_engine_version: str
    competitor_entity_key: str | None
    provider: LLMProvider | None
    prompt_id: uuid.UUID | None
    prompt_type: PromptType
    title: str
    summary: str
    recommended_action: str
    first_detected_scan_id: uuid.UUID
    latest_detected_scan_id: uuid.UUID
    first_detected_at: datetime
    last_detected_at: datetime
    implemented_at: datetime | None
    dismissed_at: datetime | None
    verified_at: datetime | None
    dismissal_reason: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class OpportunityDetailResponse(OpportunityResponse):
    """Extended response with latest occurrence and history."""

    latest_occurrence: OpportunityOccurrenceResponse | None = None
    occurrence_count: int = 0
    reliability_context: ReliabilityContextResponse | None = None


class OpportunityListResponse(BaseModel):
    items: list[OpportunityResponse]
    total: int
    offset: int
    limit: int


class RefreshActionsResponse(BaseModel):
    action_engine_version: str
    scan_id: uuid.UUID
    opportunities_detected: int
    opportunities_created: int
    opportunities_updated: int
    occurrences_created: int
    warnings: list[str]


class OpportunityStatusUpdateRequest(BaseModel):
    status: OpportunityStatus
    dismissal_reason: str | None = None
