"""Phase 10 Verification Scans and Opportunity Outcome Tracking API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.core.enums import (
    OpportunityStatus,
    ScanStatus,
    ScanType,
    VerificationOutcome,
    VerificationReasonCode,
)


class VerificationScanCreateRequest(BaseModel):
    """Request body for creating a verification scan."""

    pass  # No parameters — the Opportunity determines everything.


class VerificationScanCreateResponse(BaseModel):
    """Response for verification scan creation."""

    scan_id: uuid.UUID
    verification_id: uuid.UUID
    opportunity_id: uuid.UUID
    baseline_scan_id: uuid.UUID
    baseline_occurrence_id: uuid.UUID
    scan_type: ScanType
    status: ScanStatus
    prompt_count: int
    provider_count: int
    planned_ai_checks: int
    verification_methodology_version: str


class VerificationResponse(BaseModel):
    """One verification comparison record."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    opportunity_id: uuid.UUID
    baseline_occurrence_id: uuid.UUID
    baseline_scan_id: uuid.UUID
    verification_scan_id: uuid.UUID
    verification_methodology_version: str
    outcome: VerificationOutcome
    reason_code: VerificationReasonCode | None
    evaluation_message: str | None
    metric_name: str
    baseline_value: Decimal | None
    verification_value: Decimal | None
    delta_value: Decimal | None
    baseline_brand_value: Decimal | None
    verification_brand_value: Decimal | None
    baseline_coverage: Decimal | None
    verification_coverage: Decimal | None
    resolution_threshold: Decimal | None
    meaningful_improvement_threshold: Decimal | None
    evaluated_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VerificationListResponse(BaseModel):
    """Paginated list of verifications for an Opportunity."""

    items: list[VerificationResponse]
    total: int
    offset: int
    limit: int


class VerificationEvaluationResponse(BaseModel):
    """Response for the deterministic evaluation endpoint."""

    verification_id: uuid.UUID
    opportunity_id: uuid.UUID
    outcome: VerificationOutcome
    reason_code: VerificationReasonCode | None
    evaluation_message: str
    metric_name: str
    baseline_value: Decimal | None
    verification_value: Decimal | None
    delta_value: Decimal | None
    baseline_brand_value: Decimal | None
    verification_brand_value: Decimal | None
    baseline_coverage: Decimal | None
    verification_coverage: Decimal | None
    resolution_threshold: Decimal | None
    meaningful_improvement_threshold: Decimal | None
    opportunity_status_after: str


class VerificationSummaryResponse(BaseModel):
    """Summary of verification outcomes for an Opportunity."""

    opportunity_id: uuid.UUID
    total_verifications: int
    resolved_count: int
    improved_count: int
    not_improved_count: int
    regressed_count: int
    inconclusive_count: int
    pending_count: int
    latest_outcome: VerificationOutcome | None
    latest_evaluated_at: datetime | None
    opportunity_status: OpportunityStatus
