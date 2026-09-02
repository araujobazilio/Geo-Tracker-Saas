"""Unit tests for web view models — label translations and formatting.

These tests verify that internal enum values are correctly mapped to
customer-facing language and that formatting helpers behave correctly.
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
from app.web.view_models import (
    format_int,
    format_metric_or_none,
    format_percent,
    opportunity_priority_badge,
    opportunity_priority_label,
    opportunity_status_badge,
    opportunity_status_label,
    opportunity_type_label,
    project_status_label,
    prompt_run_status_label,
    provider_label,
    scan_status_badge,
    scan_status_label,
    scan_type_label,
    schedule_outcome_label,
    truncate,
    verification_outcome_explanation,
    verification_outcome_label,
)


class TestProviderLabels:
    def test_known_providers(self) -> None:
        assert provider_label(LLMProvider.OPENAI) == "OpenAI"
        assert provider_label(LLMProvider.ANTHROPIC) == "Anthropic"
        assert provider_label(LLMProvider.GOOGLE) == "Google"
        assert provider_label(LLMProvider.PERPLEXITY) == "Perplexity"

    def test_string_provider(self) -> None:
        assert provider_label("OPENAI") == "OpenAI"
        assert provider_label("ANTHROPIC") == "Anthropic"

    def test_unknown_string_provider(self) -> None:
        assert provider_label("unknown_provider") == "unknown_provider"


class TestScanLabels:
    def test_scan_type_labels(self) -> None:
        assert scan_type_label(ScanType.STANDARD) == "Measurement"
        assert scan_type_label(ScanType.CONFIDENCE) == "Reliability check"
        assert scan_type_label(ScanType.VERIFICATION) == "Verification"

    def test_scan_type_string(self) -> None:
        assert scan_type_label("STANDARD") == "Measurement"
        assert scan_type_label("VERIFICATION") == "Verification"

    def test_scan_status_labels(self) -> None:
        assert scan_status_label(ScanStatus.PENDING) == "Queued"
        assert scan_status_label(ScanStatus.RUNNING) == "Running"
        assert scan_status_label(ScanStatus.COMPLETED) == "Completed"
        assert scan_status_label(ScanStatus.FAILED) == "Failed"

    def test_scan_status_badges(self) -> None:
        assert scan_status_badge(ScanStatus.PENDING) == "badge-gray"
        assert scan_status_badge(ScanStatus.RUNNING) == "badge-blue"
        assert scan_status_badge(ScanStatus.COMPLETED) == "badge-green"
        assert scan_status_badge(ScanStatus.FAILED) == "badge-red"

    def test_scan_status_string(self) -> None:
        assert scan_status_label("PENDING") == "Queued"
        assert scan_status_badge("RUNNING") == "badge-blue"

    def test_unknown_scan_status_badge(self) -> None:
        assert scan_status_badge("unknown") == "badge-gray"


class TestPromptRunLabels:
    def test_known_statuses(self) -> None:
        assert prompt_run_status_label(PromptRunStatus.PENDING) == "Queued"
        assert prompt_run_status_label(PromptRunStatus.RUNNING) == "Running"
        assert prompt_run_status_label(PromptRunStatus.SUCCEEDED) == "Completed"
        assert prompt_run_status_label(PromptRunStatus.FAILED) == "Measurement unavailable"

    def test_string_status(self) -> None:
        assert prompt_run_status_label("SUCCEEDED") == "Completed"


class TestOpportunityLabels:
    def test_priority_labels(self) -> None:
        assert opportunity_priority_label(OpportunityPriority.HIGH) == "High"
        assert opportunity_priority_label(OpportunityPriority.MEDIUM) == "Medium"
        assert opportunity_priority_label(OpportunityPriority.LOW) == "Low"

    def test_priority_badges(self) -> None:
        assert opportunity_priority_badge(OpportunityPriority.HIGH) == "badge-red"
        assert opportunity_priority_badge(OpportunityPriority.MEDIUM) == "badge-yellow"
        assert opportunity_priority_badge(OpportunityPriority.LOW) == "badge-gray"

    def test_status_labels(self) -> None:
        assert opportunity_status_label(OpportunityStatus.OPEN) == "Open"
        assert opportunity_status_label(OpportunityStatus.IN_PROGRESS) == "In progress"
        assert opportunity_status_label(OpportunityStatus.IMPLEMENTED) == "Implemented"
        assert opportunity_status_label(OpportunityStatus.VERIFIED) == "Verified"
        assert opportunity_status_label(OpportunityStatus.DISMISSED) == "Dismissed"

    def test_status_badges(self) -> None:
        assert opportunity_status_badge(OpportunityStatus.OPEN) == "badge-blue"
        assert opportunity_status_badge(OpportunityStatus.VERIFIED) == "badge-green"

    def test_type_labels(self) -> None:
        assert opportunity_type_label("DISCOVERY_VISIBILITY_GAP") == "Discovery visibility gap"
        assert opportunity_type_label("PROVIDER_VISIBILITY_GAP") == "Provider visibility gap"
        assert opportunity_type_label("OWNED_CITATION_GAP") == "Citation gap"

    def test_unknown_type_label(self) -> None:
        result = opportunity_type_label("SOME_UNKNOWN_TYPE")
        assert "Some Unknown Type" in result


class TestVerificationLabels:
    def test_outcome_labels(self) -> None:
        assert verification_outcome_label(VerificationOutcome.PENDING) == "Pending"
        assert verification_outcome_label(VerificationOutcome.RESOLVED) == "Resolved"
        assert verification_outcome_label(VerificationOutcome.IMPROVED) == "Improved"
        assert verification_outcome_label(VerificationOutcome.NOT_IMPROVED) == "No improvement"
        assert verification_outcome_label(VerificationOutcome.REGRESSED) == "Regressed"
        assert verification_outcome_label(VerificationOutcome.INCONCLUSIVE) == "Inconclusive"

    def test_outcome_explanations(self) -> None:
        assert "no longer meets" in verification_outcome_explanation(VerificationOutcome.RESOLVED)
        assert "improved" in verification_outcome_explanation(VerificationOutcome.IMPROVED).lower()
        assert "worsened" in verification_outcome_explanation(VerificationOutcome.REGRESSED)

    def test_pending_explanation(self) -> None:
        assert (
            "in progress" in verification_outcome_explanation(VerificationOutcome.PENDING).lower()
        )


class TestScheduleLabels:
    def test_skip_labels(self) -> None:
        assert "AI Checks" in schedule_outcome_label(ScheduledScanOutcome.SKIPPED_QUOTA)
        assert "plan" in schedule_outcome_label(ScheduledScanOutcome.SKIPPED_ENTITLEMENT).lower()
        assert "started" in schedule_outcome_label(ScheduledScanOutcome.TRIGGERED).lower()


class TestProjectLabels:
    def test_status_labels(self) -> None:
        assert project_status_label(ProjectStatus.ACTIVE) == "Active"
        assert project_status_label(ProjectStatus.PAUSED) == "Paused"
        assert project_status_label(ProjectStatus.ARCHIVED) == "Archived"


class TestFormatting:
    def test_format_percent_none(self) -> None:
        assert format_percent(None) == "Not enough data"

    def test_format_percent_decimal(self) -> None:
        assert format_percent(Decimal("42.5")) == "42.5%"

    def test_format_percent_float(self) -> None:
        assert format_percent(42.5) == "42.5%"

    def test_format_metric_or_none_none(self) -> None:
        assert format_metric_or_none(None) == "Not enough data"

    def test_format_metric_or_none_decimal(self) -> None:
        assert format_metric_or_none(Decimal("10.0")) == "10.0%"

    def test_format_metric_or_none_with_suffix(self) -> None:
        assert format_metric_or_none(Decimal("5.0"), suffix=" pp") == "5.0 pp"

    def test_format_int_none(self) -> None:
        assert format_int(None) == "0"

    def test_format_int_value(self) -> None:
        assert format_int(42) == "42"

    def test_truncate_short(self) -> None:
        assert truncate("short text") == "short text"

    def test_truncate_long(self) -> None:
        result = truncate("a" * 100, max_len=60)
        assert len(result) == 60
        assert result.endswith("…")
