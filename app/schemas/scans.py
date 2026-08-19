"""Customer-safe Scan and PromptRun API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.enums import (
    LLMProvider,
    PromptRunStatus,
    ProviderErrorCode,
    ProviderExecutionMode,
    ProviderSurface,
    ScanStatus,
    ScanType,
)


class ScanCreateRequest(BaseModel):
    scan_type: ScanType = ScanType.STANDARD


class ScanSummaryResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    prompt_set_id: uuid.UUID
    prompt_set_version: int
    scan_type: ScanType
    status: ScanStatus
    prompt_count: int
    provider_count: int
    planned_ai_checks: int
    successful_runs: int
    failed_runs: int
    providers: list[LLMProvider]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ScanListResponse(BaseModel):
    items: list[ScanSummaryResponse]
    offset: int
    limit: int


class ResponseSourceResponse(BaseModel):
    ordinal: int
    url: str
    title: str | None
    source_type: str | None
    start_index: int | None
    end_index: int | None
    cited_text: str | None

    model_config = {"from_attributes": True}


class PromptRunSummaryResponse(BaseModel):
    id: uuid.UUID
    prompt_id: uuid.UUID
    provider: LLMProvider
    provider_surface: ProviderSurface
    execution_mode: ProviderExecutionMode
    requested_model: str
    returned_model: str | None
    status: PromptRunStatus
    attempt_number: int
    response_text: str | None
    provider_request_id: str | None
    provider_response_id: str | None
    finish_reason: str | None
    latency_ms: int | None
    search_used: bool | None
    error_code: ProviderErrorCode | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PromptRunDetailResponse(PromptRunSummaryResponse):
    sources: list[ResponseSourceResponse]


class ScanDetailResponse(ScanSummaryResponse):
    runs: list[PromptRunSummaryResponse]


class ScanPaginationParams(BaseModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)
