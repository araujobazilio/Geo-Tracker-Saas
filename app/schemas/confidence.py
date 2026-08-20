"""Confidence Scan API schemas (Phase 8)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.core.enums import LLMProvider, PromptType, ScanStatus, ScanType


class ConfidenceScanCreateRequest(BaseModel):
    repeat_count: int | None = Field(default=None, ge=2, le=10)


class ConfidenceScanCreateResponse(BaseModel):
    scan_id: uuid.UUID
    baseline_scan_id: uuid.UUID
    scan_type: ScanType
    repeat_count: int
    prompt_count: int
    provider_count: int
    planned_ai_checks: int
    status: ScanStatus


class RoundSummaryResponse(BaseModel):
    observation_index: int
    planned_observations: int
    successful_observations: int
    measurement_coverage: Decimal | None
    entity_visibility: dict[str, Decimal | None]


class EntityReliabilityResponse(BaseModel):
    entity_snapshot_id: uuid.UUID
    entity_type: str
    name: str
    domain: str
    overall_visibility_rate: Decimal | None
    planned_cells: int
    repeat_analyzable_cells: int
    stable_cells: int
    variable_cells: int
    insufficient_cells: int
    repeat_sufficiency: Decimal | None
    mention_stability: Decimal | None
    observed_visibility_min: Decimal | None
    observed_visibility_max: Decimal | None
    observed_visibility_range: Decimal | None
    confidence_level: str


class ProviderReliabilityResponse(BaseModel):
    provider: LLMProvider
    planned_observations: int
    successful_observations: int
    measurement_coverage: Decimal | None
    brand_visibility_rate: Decimal | None
    observed_visibility_min: Decimal | None
    observed_visibility_max: Decimal | None
    repeat_sufficiency: Decimal | None
    mention_stability: Decimal | None
    confidence_level: str


class ConfidenceMetricsResponse(BaseModel):
    scan_id: uuid.UUID
    baseline_scan_id: uuid.UUID | None
    repeat_count: int
    confidence_methodology_version: str
    scope: PromptType
    provider_filter: LLMProvider | None
    planned_observations: int
    successful_observations: int
    measurement_coverage: Decimal | None
    overall_confidence_level: str
    round_summaries: list[RoundSummaryResponse]
    entity_reliability: list[EntityReliabilityResponse]
    provider_breakdown: list[ProviderReliabilityResponse]
