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
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_REQUEST = "INVALID_REQUEST"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    SEARCH_ERROR = "SEARCH_ERROR"
    MODE_NOT_ALLOWED = "MODE_NOT_ALLOWED"
    ACCOUNTING_ERROR = "ACCOUNTING_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"


class PromptRunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class CostSource(str, Enum):
    PROVIDER_REPORTED = "PROVIDER_REPORTED"
    PRICE_RULE = "PRICE_RULE"
    UNKNOWN = "UNKNOWN"


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


class ProviderSurface(str, Enum):
    """Identifies the specific API surface that produced a provider result.

    This is distinct from LLMProvider (which identifies the company).
    A provider surface is the specific API endpoint/protocol used.

    API results are measurements of their named provider surfaces, NOT
    necessarily identical to consumer-facing products (ChatGPT, Claude.ai,
    Gemini UI, Perplexity UI).
    """

    OPENAI_RESPONSES_API = "OPENAI_RESPONSES_API"
    ANTHROPIC_MESSAGES_API = "ANTHROPIC_MESSAGES_API"
    GOOGLE_INTERACTIONS_API = "GOOGLE_INTERACTIONS_API"
    PERPLEXITY_SONAR_API = "PERPLEXITY_SONAR_API"


class ProviderExecutionMode(str, Enum):
    """Execution mode for a provider request.

    MODEL_ONLY: Send the prompt to the model without web search grounding.
    WEB_GROUNDED: Use the provider's web search tool to ground the response.

    Not all providers support all modes. Requesting an unsupported mode
    raises ProviderModeNotAllowedError BEFORE any network call.
    """

    MODEL_ONLY = "MODEL_ONLY"
    WEB_GROUNDED = "WEB_GROUNDED"


class TrackedEntityType(str, Enum):
    """Type of entity tracked in a ScanEntitySnapshot."""

    BRAND = "BRAND"
    COMPETITOR = "COMPETITOR"


class ScanAnalysisStatus(str, Enum):
    """Lifecycle states for a ScanAnalysis."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EntityMatchType(str, Enum):
    """How an entity term was matched in response text."""

    NAME = "NAME"
    ALIAS = "ALIAS"
    DOMAIN = "DOMAIN"


class AttributionType(str, Enum):
    """How a ResponseSource was attributed to an entity."""

    OWNED_DOMAIN = "OWNED_DOMAIN"


class OpportunityType(str, Enum):
    """Type of evidence-based opportunity detected by the Action Engine."""

    DISCOVERY_VISIBILITY_GAP = "DISCOVERY_VISIBILITY_GAP"
    PROVIDER_VISIBILITY_GAP = "PROVIDER_VISIBILITY_GAP"
    OWNED_CITATION_GAP = "OWNED_CITATION_GAP"
    PROMPT_COMPETITOR_GAP = "PROMPT_COMPETITOR_GAP"


class OpportunityPriority(str, Enum):
    """Deterministic priority for an Opportunity (not ML scoring)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class OpportunityEvidenceType(str, Enum):
    """Type of evidence backing an Opportunity occurrence."""

    METRIC_GAP = "METRIC_GAP"
    PROMPT_RUN = "PROMPT_RUN"
    OWNED_SOURCE = "OWNED_SOURCE"


class VerificationOutcome(str, Enum):
    """Deterministic outcome of an Opportunity verification comparison.

    PENDING: verification exists but evaluation has not completed.
    RESOLVED: the original measured issue falls below its Action Engine
        trigger threshold and passes verification quality gates.
    IMPROVED: the issue materially improved but is still above its
        resolution threshold.
    NOT_IMPROVED: change is smaller than the configured meaningful-
        improvement threshold.
    REGRESSED: the measured issue materially worsened.
    INCONCLUSIVE: the new measurement cannot support a reliable
        comparison because of coverage, analysis, missing eligible
        evidence, or another evidence-quality condition.

    SUCCESS and FAILURE are intentionally NOT included — they imply a
    stronger causal interpretation than the deterministic before/after
    comparison supports.
    """

    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    IMPROVED = "IMPROVED"
    NOT_IMPROVED = "NOT_IMPROVED"
    REGRESSED = "REGRESSED"
    INCONCLUSIVE = "INCONCLUSIVE"


class VerificationReasonCode(str, Enum):
    """Bounded deterministic reason codes for INCONCLUSIVE outcomes.

    These are customer-safe codes — never store raw stack traces in
    customer-facing fields.
    """

    INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"
    INSUFFICIENT_BASELINE_COVERAGE = "INSUFFICIENT_BASELINE_COVERAGE"
    ANALYSIS_NOT_COMPLETED = "ANALYSIS_NOT_COMPLETED"
    NO_SUCCESSFUL_OBSERVATIONS = "NO_SUCCESSFUL_OBSERVATIONS"
    INSUFFICIENT_CITATION_EVIDENCE = "INSUFFICIENT_CITATION_EVIDENCE"
    BASELINE_EVIDENCE_UNAVAILABLE = "BASELINE_EVIDENCE_UNAVAILABLE"
