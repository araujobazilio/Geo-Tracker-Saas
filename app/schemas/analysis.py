"""Phase 7 analysis and metrics API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.core.enums import (
    AttributionType,
    EntityMatchType,
    LLMProvider,
    PromptType,
    ScanAnalysisStatus,
    ScanStatus,
    TrackedEntityType,
)


class EntitySnapshotResponse(BaseModel):
    id: uuid.UUID
    entity_key: str
    entity_type: TrackedEntityType
    name: str
    domain: str
    aliases: list[str]
    ordinal: int

    model_config = {"from_attributes": True}


class AnalysisResponse(BaseModel):
    id: uuid.UUID
    scan_id: uuid.UUID
    analysis_version: str
    status: ScanAnalysisStatus
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None
    failure_message: str | None
    warning_count: int
    created_at: datetime
    entity_snapshots: list[EntitySnapshotResponse]

    model_config = {"from_attributes": True}


class EntityMentionResponse(BaseModel):
    id: uuid.UUID
    entity_snapshot_id: uuid.UUID
    occurrence_index: int
    match_type: EntityMatchType
    matched_text: str
    matched_term: str
    start_index: int
    end_index: int

    model_config = {"from_attributes": True}


class SourceAttributionResponse(BaseModel):
    id: uuid.UUID
    entity_snapshot_id: uuid.UUID
    response_source_id: uuid.UUID
    source_host: str
    attribution_type: AttributionType

    model_config = {"from_attributes": True}


class RunAnalysisResponse(BaseModel):
    prompt_run_id: uuid.UUID
    mentions: list[EntityMentionResponse]
    attributions: list[SourceAttributionResponse]


class EntityMetricResponse(BaseModel):
    entity_snapshot_id: uuid.UUID
    entity_type: str
    name: str
    domain: str
    planned_observations: int
    successful_observations: int
    mentioned_observations: int
    visibility_rate: Decimal | None
    share_of_voice: Decimal | None
    citation_eligible_observations: int
    owned_cited_observations: int
    owned_source_count: int
    owned_citation_rate: Decimal | None
    owned_source_share: Decimal | None


class ProviderBreakdownResponse(BaseModel):
    provider: LLMProvider
    successful_observations: int
    planned_observations: int
    measurement_coverage: Decimal | None
    visibility_rate: Decimal | None
    citation_eligible_observations: int
    owned_citation_rate: Decimal | None


class MetricsResponse(BaseModel):
    scan_id: uuid.UUID
    scan_status: ScanStatus
    analysis_version: str | None
    analysis_status: ScanAnalysisStatus | None
    prompt_set_version: int
    scope: PromptType
    provider_filter: LLMProvider | None
    planned_observations: int
    successful_observations: int
    measurement_coverage: Decimal | None
    entity_metrics: list[EntityMetricResponse]
    provider_breakdown: list[ProviderBreakdownResponse]
    leaderboard: list[EntityMetricResponse]
