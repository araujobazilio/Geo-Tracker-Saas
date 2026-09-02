"""View models — customer-friendly label translations.

Maps internal enum values to customer-facing language. Never exposes
internal terminology like PromptRun, UsageEvent, QuotaReservation, etc.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.enums import (
    LLMProvider,
    OpportunityPriority,
    OpportunityStatus,
    ProjectStatus,
    PromptRunStatus,
    ScanStatus,
    ScanType,
    ScheduledScanOutcome,
    VerificationOutcome,
)

# --- Provider labels ---

_PROVIDER_LABELS: dict[LLMProvider, str] = {
    LLMProvider.OPENAI: "OpenAI",
    LLMProvider.ANTHROPIC: "Anthropic",
    LLMProvider.GOOGLE: "Google",
    LLMProvider.PERPLEXITY: "Perplexity",
}


def provider_label(provider: LLMProvider | str) -> str:
    """Return the customer-facing provider name."""
    if isinstance(provider, str):
        try:
            provider = LLMProvider(provider)
        except ValueError:
            return str(provider)
    return _PROVIDER_LABELS.get(provider, provider.value)


# --- Scan labels ---

_SCAN_TYPE_LABELS: dict[ScanType, str] = {
    ScanType.STANDARD: "Measurement",
    ScanType.CONFIDENCE: "Reliability check",
    ScanType.VERIFICATION: "Verification",
}

_SCAN_STATUS_LABELS: dict[ScanStatus, str] = {
    ScanStatus.PENDING: "Queued",
    ScanStatus.RUNNING: "Running",
    ScanStatus.COMPLETED: "Completed",
    ScanStatus.PARTIAL: "Partial",
    ScanStatus.FAILED: "Failed",
    ScanStatus.CANCELED: "Canceled",
}

_SCAN_STATUS_BADGE: dict[ScanStatus, str] = {
    ScanStatus.PENDING: "badge-gray",
    ScanStatus.RUNNING: "badge-blue",
    ScanStatus.COMPLETED: "badge-green",
    ScanStatus.PARTIAL: "badge-yellow",
    ScanStatus.FAILED: "badge-red",
    ScanStatus.CANCELED: "badge-gray",
}


def scan_type_label(scan_type: ScanType | str) -> str:
    if isinstance(scan_type, str):
        try:
            scan_type = ScanType(scan_type)
        except ValueError:
            return str(scan_type)
    return _SCAN_TYPE_LABELS.get(scan_type, scan_type.value)


def scan_status_label(status: ScanStatus | str) -> str:
    if isinstance(status, str):
        try:
            status = ScanStatus(status)
        except ValueError:
            return str(status)
    return _SCAN_STATUS_LABELS.get(status, status.value)


def scan_status_badge(status: ScanStatus | str) -> str:
    if isinstance(status, str):
        try:
            status = ScanStatus(status)
        except ValueError:
            return "badge-gray"
    return _SCAN_STATUS_BADGE.get(status, "badge-gray")


# --- PromptRun labels ---

_PROMPT_RUN_STATUS_LABELS: dict[PromptRunStatus, str] = {
    PromptRunStatus.PENDING: "Queued",
    PromptRunStatus.RUNNING: "Running",
    PromptRunStatus.SUCCEEDED: "Completed",
    PromptRunStatus.FAILED: "Measurement unavailable",
}


def prompt_run_status_label(status: PromptRunStatus | str) -> str:
    if isinstance(status, str):
        try:
            status = PromptRunStatus(status)
        except ValueError:
            return str(status)
    return _PROMPT_RUN_STATUS_LABELS.get(status, status.value)


# --- Opportunity labels ---

_OPPORTUNITY_PRIORITY_LABELS: dict[OpportunityPriority, str] = {
    OpportunityPriority.HIGH: "High",
    OpportunityPriority.MEDIUM: "Medium",
    OpportunityPriority.LOW: "Low",
}

_OPPORTUNITY_PRIORITY_BADGE: dict[OpportunityPriority, str] = {
    OpportunityPriority.HIGH: "badge-red",
    OpportunityPriority.MEDIUM: "badge-yellow",
    OpportunityPriority.LOW: "badge-gray",
}

_OPPORTUNITY_STATUS_LABELS: dict[OpportunityStatus, str] = {
    OpportunityStatus.OPEN: "Open",
    OpportunityStatus.IN_PROGRESS: "In progress",
    OpportunityStatus.IMPLEMENTED: "Implemented",
    OpportunityStatus.VERIFIED: "Verified",
    OpportunityStatus.DISMISSED: "Dismissed",
}

_OPPORTUNITY_STATUS_BADGE: dict[OpportunityStatus, str] = {
    OpportunityStatus.OPEN: "badge-blue",
    OpportunityStatus.IN_PROGRESS: "badge-yellow",
    OpportunityStatus.IMPLEMENTED: "badge-purple",
    OpportunityStatus.VERIFIED: "badge-green",
    OpportunityStatus.DISMISSED: "badge-gray",
}

_OPPORTUNITY_TYPE_LABELS: dict[str, str] = {
    "DISCOVERY_VISIBILITY_GAP": "Discovery visibility gap",
    "PROVIDER_VISIBILITY_GAP": "Provider visibility gap",
    "OWNED_CITATION_GAP": "Citation gap",
    "PROMPT_COMPETITOR_GAP": "Competitor visibility gap",
}


def opportunity_priority_label(priority: OpportunityPriority | str) -> str:
    if isinstance(priority, str):
        try:
            priority = OpportunityPriority(priority)
        except ValueError:
            return str(priority)
    return _OPPORTUNITY_PRIORITY_LABELS.get(priority, priority.value)


def opportunity_priority_badge(priority: OpportunityPriority | str) -> str:
    if isinstance(priority, str):
        try:
            priority = OpportunityPriority(priority)
        except ValueError:
            return "badge-gray"
    return _OPPORTUNITY_PRIORITY_BADGE.get(priority, "badge-gray")


def opportunity_status_label(status: OpportunityStatus | str) -> str:
    if isinstance(status, str):
        try:
            status = OpportunityStatus(status)
        except ValueError:
            return str(status)
    return _OPPORTUNITY_STATUS_LABELS.get(status, status.value)


def opportunity_status_badge(status: OpportunityStatus | str) -> str:
    if isinstance(status, str):
        try:
            status = OpportunityStatus(status)
        except ValueError:
            return "badge-gray"
    return _OPPORTUNITY_STATUS_BADGE.get(status, "badge-gray")


def opportunity_type_label(opp_type: str) -> str:
    return _OPPORTUNITY_TYPE_LABELS.get(opp_type, opp_type.replace("_", " ").title())


# --- Verification labels ---

_VERIFICATION_OUTCOME_LABELS: dict[VerificationOutcome, str] = {
    VerificationOutcome.PENDING: "Pending",
    VerificationOutcome.RESOLVED: "Resolved",
    VerificationOutcome.IMPROVED: "Improved",
    VerificationOutcome.NOT_IMPROVED: "No improvement",
    VerificationOutcome.REGRESSED: "Regressed",
    VerificationOutcome.INCONCLUSIVE: "Inconclusive",
}

_VERIFICATION_OUTCOME_EXPLANATIONS: dict[VerificationOutcome, str] = {
    VerificationOutcome.RESOLVED: (
        "The measured issue no longer meets the threshold that originally created this opportunity."
    ),
    VerificationOutcome.IMPROVED: (
        "The measured issue materially improved but is still above the resolution threshold."
    ),
    VerificationOutcome.NOT_IMPROVED: (
        "The change was smaller than the meaningful-improvement threshold."
    ),
    VerificationOutcome.REGRESSED: "The measured issue materially worsened.",
    VerificationOutcome.INCONCLUSIVE: (
        "The new measurement could not support a reliable comparison."
    ),
    VerificationOutcome.PENDING: "Verification is in progress.",
}


def verification_outcome_label(outcome: VerificationOutcome | str) -> str:
    if isinstance(outcome, str):
        try:
            outcome = VerificationOutcome(outcome)
        except ValueError:
            return str(outcome)
    return _VERIFICATION_OUTCOME_LABELS.get(outcome, outcome.value)


def verification_outcome_explanation(outcome: VerificationOutcome | str) -> str:
    if isinstance(outcome, str):
        try:
            outcome = VerificationOutcome(outcome)
        except ValueError:
            return ""
    return _VERIFICATION_OUTCOME_EXPLANATIONS.get(outcome, "")


# --- Schedule labels ---

_SCHEDULE_SKIP_LABELS: dict[ScheduledScanOutcome, str] = {
    ScheduledScanOutcome.SKIPPED_QUOTA: (
        "Skipped because the workspace did not have enough AI Checks."
    ),
    ScheduledScanOutcome.SKIPPED_ENTITLEMENT: "Paused by the current plan limits.",
    ScheduledScanOutcome.SKIPPED_ACTIVE_SCAN: (
        "Skipped because a previous scheduled measurement was still running."
    ),
    ScheduledScanOutcome.SKIPPED_PROJECT_INACTIVE: "Skipped because the project is not active.",
    ScheduledScanOutcome.SKIPPED_NOT_READY: "Skipped because the schedule is not ready.",
    ScheduledScanOutcome.TRIGGERED: "Measurement started.",
    ScheduledScanOutcome.DISPATCH_FAILED: "Failed to start the measurement.",
}


def schedule_outcome_label(outcome: ScheduledScanOutcome | str) -> str:
    if isinstance(outcome, str):
        try:
            outcome = ScheduledScanOutcome(outcome)
        except ValueError:
            return str(outcome)
    return _SCHEDULE_SKIP_LABELS.get(outcome, outcome.value)


# --- Project labels ---

_PROJECT_STATUS_LABELS: dict[ProjectStatus, str] = {
    ProjectStatus.ACTIVE: "Active",
    ProjectStatus.PAUSED: "Paused",
    ProjectStatus.ARCHIVED: "Archived",
}


def project_status_label(status: ProjectStatus | str) -> str:
    if isinstance(status, str):
        try:
            status = ProjectStatus(status)
        except ValueError:
            return str(status)
    return _PROJECT_STATUS_LABELS.get(status, status.value)


# --- Metric formatting ---


def format_percent(value: Decimal | float | None) -> str:
    """Format a percentage value. Returns 'Not enough data' for None."""
    if value is None:
        return "Not enough data"
    if isinstance(value, Decimal):
        return f"{value:.1f}%"
    return f"{value:.1f}%"


def format_metric_or_none(value: Decimal | float | None, suffix: str = "%") -> str:
    """Format a metric value, returning 'Not enough data' for None, '0.0%' for 0."""
    if value is None:
        return "Not enough data"
    if isinstance(value, Decimal):
        return f"{value:.1f}{suffix}"
    return f"{value:.1f}{suffix}"


def format_int(value: int | None) -> str:
    """Format an integer, returning '0' for None."""
    if value is None:
        return "0"
    return str(value)


def truncate(text: str, max_len: int = 60) -> str:
    """Truncate text with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"
