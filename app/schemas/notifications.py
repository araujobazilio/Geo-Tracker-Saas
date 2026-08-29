"""Pydantic schemas for schedule, notification, and preference APIs."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

# --- Schedule ---


class ScheduleCreateRequest(BaseModel):
    """PUT body for creating/replacing a schedule."""

    enabled: bool = True
    interval_hours: int = Field(..., gt=0)
    first_run_at: datetime | None = None


class ScheduleResponse(BaseModel):
    """Schedule response — exposes minimum_allowed_interval_hours."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    enabled: bool
    interval_hours: int
    minimum_allowed_interval_hours: int | None
    next_run_at: datetime
    last_due_at: datetime | None = None
    last_triggered_at: datetime | None = None
    last_scan_id: uuid.UUID | None = None
    last_outcome: str | None = None
    last_skip_reason: str | None = None
    created_by_user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Notifications ---


class NotificationResponse(BaseModel):
    """Notification response for API consumers."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    notification_type: str
    title: str
    message: str
    project_id: uuid.UUID | None = None
    scan_id: uuid.UUID | None = None
    opportunity_id: uuid.UUID | None = None
    verification_id: uuid.UUID | None = None
    deep_link_path: str | None = None
    read_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationListResponse(BaseModel):
    """Paginated notification list."""

    items: list[NotificationResponse]
    total: int
    has_more: bool


class MarkReadRequest(BaseModel):
    """Mark a single notification as read."""

    pass


# --- Notification Preferences ---


class NotificationPreferenceResponse(BaseModel):
    """Notification preference response."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    email_enabled: bool
    scheduled_scan_summary: bool
    high_priority_opportunities: bool
    verification_outcomes: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NotificationPreferenceUpdateRequest(BaseModel):
    """PUT body for updating notification preferences."""

    email_enabled: bool = True
    scheduled_scan_summary: bool = True
    high_priority_opportunities: bool = True
    verification_outcomes: bool = True


# --- Email Retry ---


class EmailRetryResponse(BaseModel):
    """Response for email retry endpoint."""

    email_delivery_id: uuid.UUID
    status: str
    message: str
