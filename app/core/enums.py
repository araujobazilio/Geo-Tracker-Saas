"""Domain enums.

Enums are declared once here and reused by models / schemas / services.
Using string enums keeps values human-readable in the database while
remaining type-safe in Python.
"""

from __future__ import annotations

from enum import Enum


class WorkspaceType(str, Enum):
    PERSONAL = "PERSONAL"
    AGENCY = "AGENCY"


class WorkspaceRole(str, Enum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class ProjectStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


class FunnelStage(str, Enum):
    AWARENESS = "AWARENESS"
    CONSIDERATION = "CONSIDERATION"
    PURCHASE = "PURCHASE"


class CompetitorSource(str, Enum):
    USER_DEFINED = "USER_DEFINED"
    SYSTEM_SUGGESTED = "SYSTEM_SUGGESTED"


class LLMProvider(str, Enum):
    OPENAI = "OPENAI"
    ANTHROPIC = "ANTHROPIC"
    GOOGLE = "GOOGLE"
    PERPLEXITY = "PERPLEXITY"


class PromptType(str, Enum):
    NON_BRANDED = "NON_BRANDED"
    BRANDED = "BRANDED"
    COMPETITOR = "COMPETITOR"


class ScanType(str, Enum):
    STANDARD = "STANDARD"
    CONFIDENCE = "CONFIDENCE"
    VERIFICATION = "VERIFICATION"


class ScanStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class BillingSource(str, Enum):
    APPSUMO = "APPSUMO"
    STRIPE = "STRIPE"
    ADMIN = "ADMIN"


class BillingAccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCELED = "CANCELED"
    TRIALING = "TRIALING"


class AppSumoLicenseStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    SUSPENDED = "SUSPENDED"


class WebhookEventStatus(str, Enum):
    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    IGNORED = "IGNORED"


class UsageEventType(str, Enum):
    AI_CHECK = "AI_CHECK"
    SCAN_STARTED = "SCAN_STARTED"
    SCAN_COMPLETED = "SCAN_COMPLETED"
    QUOTA_CHECK = "QUOTA_CHECK"


class EntityType(str, Enum):
    PRIMARY_BRAND = "PRIMARY_BRAND"
    COMPETITOR = "COMPETITOR"


class OpportunityStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    IMPLEMENTED = "IMPLEMENTED"
    VERIFIED = "VERIFIED"
    DISMISSED = "DISMISSED"


class ProviderErrorCode(str, Enum):
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_REQUEST = "INVALID_REQUEST"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"


class QuotaReservationStatus(str, Enum):
    """Lifecycle states for a QuotaReservation."""

    ACTIVE = "ACTIVE"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class PromptSetStatus(str, Enum):
    """Lifecycle states for a PromptSet."""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
