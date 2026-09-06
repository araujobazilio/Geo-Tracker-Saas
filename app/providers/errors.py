"""Provider error taxonomy.

Domain exceptions for provider adapter failures. These are distinct from
the general AppError hierarchy because provider errors are infrastructure-
level concerns that the Scan Engine (Phase 6) will handle with retry logic.

Key principles:
- Error messages must NEVER contain API keys, Authorization headers,
  or full response bodies.
- ProviderRateLimitError may include retry_after_seconds if safely parsed.
- No raw HTTP library exception should leak through normal service use.
- ProviderResponseError and ProviderSearchError may carry a
  ProviderFailureEvidence object when the provider returned a billable
  response envelope but the functional result is unusable (e.g. empty
  output_text, incomplete status, missing search).  The evidence allows
  the Scan Engine to persist usage/cost/IDs even on FAILED PromptRuns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.providers.base import ProviderFailureEvidence


class ProviderError(Exception):
    """Base exception for all provider adapter failures."""

    def __init__(
        self,
        message: str = "",
        *,
        provider: str = "",
        evidence: ProviderFailureEvidence | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.evidence = evidence

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
        evidence: ProviderFailureEvidence | None = None,
    ) -> None:
        super().__init__(message, provider=provider, evidence=evidence)
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


class ProviderContractViolationError(ProviderError):
    """The provider returned a billable response that violated the execution
    contract requested by GEO Tracker (e.g. more web_search_call items than
    the configured max_tool_calls limit).

    This is raised AFTER a successful HTTP 200 response with a valid envelope.
    The exception carries ProviderFailureEvidence so the Scan Engine can
    persist usage/cost/IDs and commit one AI Check, even though the PromptRun
    is marked FAILED.

    The violation is NOT a malformed response — the provider returned a
    parseable envelope.  It is a contract violation: the provider did not
    respect the bounds of the execution request.
    """
