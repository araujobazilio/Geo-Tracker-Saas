"""Pydantic schemas for project onboarding and management endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.core.enums import FunnelStage, LLMProvider, ProjectStatus, PromptSetStatus, PromptType

# --- Keyword schemas ---


class KeywordCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    intent: str | None = Field(default=None, max_length=255)
    funnel_stage: FunnelStage | None = None


class KeywordUpdateRequest(BaseModel):
    intent: str | None = Field(default=None, max_length=255)
    funnel_stage: FunnelStage | None = None
    active: bool | None = None


class KeywordResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    text: str
    normalized_text: str
    intent: str | None
    funnel_stage: FunnelStage | None
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Competitor schemas ---


class CompetitorCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    domain: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list, max_length=50)


class CompetitorUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    aliases: list[str] | None = Field(default=None, max_length=50)
    active: bool | None = None


class CompetitorResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    domain: str
    aliases: list[str]
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Provider schemas ---


class ProviderUpdateRequest(BaseModel):
    providers: list[LLMProvider] = Field(min_length=1)


class ProviderResponse(BaseModel):
    provider: LLMProvider
    enabled: bool
    allowed_by_plan: bool

    model_config = {"from_attributes": True}


# --- Project schemas ---


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    domain: str = Field(min_length=1, max_length=255)
    brand_name: str = Field(min_length=1, max_length=255)
    brand_aliases: list[str] = Field(default_factory=list, max_length=50)
    industry: str | None = Field(default=None, max_length=255)
    target_country: str | None = Field(default=None, max_length=10)
    target_language: str | None = Field(default=None, max_length=10)
    target_audience: str | None = Field(default=None, max_length=255)
    keywords: list[KeywordCreateRequest] = Field(min_length=1)
    competitors: list[CompetitorCreateRequest] = Field(default_factory=list)
    providers: list[LLMProvider] = Field(min_length=1)


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    domain: str | None = Field(default=None, min_length=1, max_length=255)
    brand_name: str | None = Field(default=None, min_length=1, max_length=255)
    brand_aliases: list[str] | None = Field(default=None, max_length=50)
    industry: str | None = Field(default=None, max_length=255)
    target_country: str | None = Field(default=None, max_length=10)
    target_language: str | None = Field(default=None, max_length=10)
    target_audience: str | None = Field(default=None, max_length=255)


class ProjectResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    domain: str
    brand_name: str
    brand_aliases: list[str]
    industry: str | None
    target_country: str | None
    target_language: str | None
    target_audience: str | None
    status: ProjectStatus
    prompt_input_revision: int
    last_scan_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectSummaryResponse(BaseModel):
    project: ProjectResponse
    keyword_count: int
    competitor_count: int
    enabled_provider_count: int
    current_prompt_set_version: int | None
    current_prompt_set_input_revision: int | None
    project_prompt_input_revision: int
    is_prompt_set_stale: bool
    standard_scan_ai_checks_estimate: int


# --- Prompt schemas ---


class PromptResponse(BaseModel):
    id: uuid.UUID
    prompt_set_id: uuid.UUID
    project_keyword_id: uuid.UUID
    variant_index: int
    text: str
    prompt_type: PromptType
    intent: str | None
    funnel_stage: FunnelStage | None
    persona: str | None
    target_country: str | None
    target_language: str | None
    commercial_intent: bool
    active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class PromptSetSummaryResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    version: int
    input_revision: int
    status: PromptSetStatus
    generator_key: str
    created_at: datetime
    activated_at: datetime | None
    prompt_count: int
    is_stale: bool

    model_config = {"from_attributes": True}


class PromptSetDetailResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    version: int
    input_revision: int
    status: PromptSetStatus
    generator_key: str
    created_at: datetime
    activated_at: datetime | None
    prompt_count: int
    is_stale: bool
    prompts: list[PromptResponse]

    model_config = {"from_attributes": True}
