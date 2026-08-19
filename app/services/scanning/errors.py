"""Stable sanitized provider failure mapping for persisted PromptRuns."""

from __future__ import annotations

from app.core.enums import ProviderErrorCode
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderBadRequestError,
    ProviderConfigurationError,
    ProviderError,
    ProviderModeNotAllowedError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderSearchError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

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
