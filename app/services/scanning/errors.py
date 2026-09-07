"""Stable sanitized provider failure mapping for persisted PromptRuns."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.enums import ProviderErrorCode
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderBadRequestError,
    ProviderConfigurationError,
    ProviderContractViolationError,
    ProviderError,
    ProviderModeNotAllowedError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderSearchError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

if TYPE_CHECKING:
    from app.providers.base import ProviderFailureEvidence

_ERROR_CODES: tuple[tuple[type[ProviderError], ProviderErrorCode], ...] = (
    (ProviderConfigurationError, ProviderErrorCode.CONFIGURATION_ERROR),
    (ProviderAuthenticationError, ProviderErrorCode.AUTHENTICATION_ERROR),
    (ProviderRateLimitError, ProviderErrorCode.RATE_LIMITED),
    (ProviderTimeoutError, ProviderErrorCode.TIMEOUT),
    (ProviderUnavailableError, ProviderErrorCode.PROVIDER_UNAVAILABLE),
    (ProviderBadRequestError, ProviderErrorCode.INVALID_REQUEST),
    (ProviderResponseError, ProviderErrorCode.MALFORMED_RESPONSE),
    (ProviderSearchError, ProviderErrorCode.SEARCH_ERROR),
    (ProviderModeNotAllowedError, ProviderErrorCode.MODE_NOT_ALLOWED),
    (ProviderContractViolationError, ProviderErrorCode.PROVIDER_CONTRACT_VIOLATION),
)


def map_provider_error(error: ProviderError) -> ProviderErrorCode:
    for error_type, code in _ERROR_CODES:
        if isinstance(error, error_type):
            return code
    return ProviderErrorCode.INTERNAL_ERROR


def safe_error_message(error: Exception) -> str:
    if isinstance(error, ProviderError):
        return (error.message or "Provider execution failed.")[:1000]
    return "Internal scan execution failure."


def enrich_error_message(
    primary_message: str,
    evidence: ProviderFailureEvidence | None,
) -> str:
    """Enrich the primary error message with durable secondary evidence.

    When multiple failure conditions co-occur (e.g. empty output AND
    max_tool_calls violation), the primary error message alone may lose
    important audit information.  This function appends secondary evidence
    fields (incomplete_reason, requested/observed max_tool_calls) to the
    primary message so they survive process exit in the persisted
    error_message column.

    Truncation policy (suffix-first):
    - Secondary evidence has PRIORITY over primary message length.
    - The suffix is built first, then primary is truncated to fit.
    - requested_max_tool_calls, observed_web_tool_call_count (or legacy
      observed_search_requests) and the per-action counters are ints and
      are NEVER truncated.
    - incomplete_reason is sanitized to max 200 chars.
    - Final result is always <= 1000 chars.

    The enriched message is:
    - deterministic (same inputs → same output)
    - sanitized (no raw response, no API key, no chain-of-thought)
    - truncated to 1000 chars
    """
    max_total = 1000

    if evidence is None:
        return primary_message[:max_total]

    # Build secondary suffix parts.
    # Priority: requested_max_tool_calls and observed_search_requests (ints,
    # never truncated) > incomplete_reason (string, sanitized to 200 chars).
    int_parts: list[str] = []
    if evidence.requested_max_tool_calls is not None:
        int_parts.append(f"requested_max_tool_calls={evidence.requested_max_tool_calls}")
    if evidence.observed_web_tool_call_count is not None:
        int_parts.append(f"observed_web_tool_call_count={evidence.observed_web_tool_call_count}")
    elif evidence.observed_search_requests is not None:
        # LEGACY fallback for evidence built without the explicit total.
        int_parts.append(f"observed_search_requests={evidence.observed_search_requests}")
    if evidence.search_action_count is not None:
        int_parts.append(f"search_action_count={evidence.search_action_count}")
    if evidence.open_page_action_count is not None:
        int_parts.append(f"open_page_action_count={evidence.open_page_action_count}")
    if evidence.find_in_page_action_count is not None:
        int_parts.append(f"find_in_page_action_count={evidence.find_in_page_action_count}")
    if evidence.unknown_web_action_count is not None:
        int_parts.append(f"unknown_web_action_count={evidence.unknown_web_action_count}")

    incomplete_reason = None
    if evidence.incomplete_reason:
        # Sanitize: truncate to 200 chars, no raw JSON/chain-of-thought.
        incomplete_reason = evidence.incomplete_reason[:200]

    # Build the suffix string.
    all_parts = list(int_parts)
    if incomplete_reason:
        all_parts.append(f"incomplete_reason={incomplete_reason}")

    if not all_parts:
        return primary_message[:max_total]

    suffix = "; ".join(all_parts) + "."
    separator = " "
    suffix_total = len(separator) + len(suffix)

    # If suffix alone exceeds max_total, preserve int parts and truncate
    # incomplete_reason further.  This should be extremely rare.
    if suffix_total > max_total:
        # Drop incomplete_reason if present, keep int parts.
        if int_parts:
            suffix = "; ".join(int_parts) + "."
            suffix_total = len(separator) + len(suffix)
        # If still too long (shouldn't happen with just ints), hard truncate.
        if suffix_total > max_total:
            return suffix[:max_total]

    # Reserve space for suffix; truncate primary.
    available_primary = max_total - suffix_total
    truncated_primary = primary_message[:available_primary]

    return f"{truncated_primary}{separator}{suffix}"
