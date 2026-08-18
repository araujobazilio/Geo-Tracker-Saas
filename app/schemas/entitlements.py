"""Pydantic schemas for entitlements and usage endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.core.enums import BillingSource, LLMProvider


class EntitlementResponse(BaseModel):
    """Public entitlements response — product capabilities, not billing internals."""

    plan_code: str
    billing_source: BillingSource | None = None
    max_projects: int
    max_keywords_per_project: int
    max_competitors_per_project: int
    max_team_members: int
    monthly_ai_checks: int
    allowed_providers: list[LLMProvider]
    min_scheduled_scan_interval_hours: int | None = None
    confidence_scans_enabled: bool
    verification_scans_enabled: bool
    white_label_reports: bool
    exports_enabled: bool
    agency_dashboard: bool
    integrations_enabled: bool
    byok_enabled: bool

    model_config = {"from_attributes": True}


class UsageResponse(BaseModel):
    """Public usage response — monthly AI Check quota state."""

    period_start: datetime
    period_end: datetime
    limit: int
    used: int
    reserved: int
    remaining: int
    usage_percentage: int

    model_config = {"from_attributes": True}
