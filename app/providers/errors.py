"""Provider error taxonomy.

Domain exceptions for provider adapter failures. These are distinct from
the general AppError hierarchy because provider errors are infrastructure-
level concerns that the Scan Engine (Phase 6) will handle with retry logic.

Key principles:
- Error messages must NEVER contain API keys, Authorization headers,
  or full response bodies.
- ProviderRateLimitError may include retry_after_seconds if safely parsed.
- No raw HTTP library exception should leak through normal service use.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base exception for all provider adapter failures."""

    def __init__(self, message: str = "", *, provider: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider

    def __str__(self) -> str:
        return self.message


class ProviderConfigurationError(ProviderError):
    """Provider is not properly configured (missing key, missing model, etc.)."""


class ProviderAuthenticationError(ProviderError):
    """Provider rejected authentication (401/403)."""


class ProviderRateLimitError(ProviderError):
    """Provider rate-limited the request (429).

    May include retry_after_seconds if safely parsed from response headers.
    """

    def __init__(
        self,
        message: str = "",
        *,
        provider: str = "",
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after_seconds = retry_after_seconds


class ProviderTimeoutError(ProviderError):
    """Provider request timed out."""


class ProviderUnavailableError(ProviderError):
    """Provider returned a 5xx error or is otherwise unavailable."""


class ProviderBadRequestError(ProviderError):
    """Provider rejected the request as invalid (400)."""


class ProviderResponseError(ProviderError):
    """Provider returned a nominally successful response that could not be
    parsed, or the response was missing required fields (e.g. empty text)."""


class ProviderSearchError(ProviderError):
    """A provider web-search tool failed inside a nominally successful
    HTTP response (e.g. Anthropic web_search_tool_result_error)."""


class ProviderModeNotAllowedError(ProviderError):
    """The requested execution mode is not supported by this provider.

    Raised BEFORE any network call. For example:
    - Google WEB_GROUNDED (compliance restriction)
    - Perplexity MODEL_ONLY (Sonar is web-grounded only)
    """
