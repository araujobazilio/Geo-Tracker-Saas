"""AI provider abstraction layer.

Provider adapters translate ProviderRequest into provider-specific HTTP
calls and normalize responses into ProviderResult.

Key design decisions:
- httpx.AsyncClient for all providers (unified transport, MockTransport tests)
- No automatic retries (Scan Engine owns retry policy)
- No quota/usage calls (Scan Engine owns accounting)
- No persistence (Scan Engine owns ProviderRun/PromptRun)
- Missing provider credentials do NOT crash application startup
"""

from __future__ import annotations

from app.providers.base import (
    ProviderAdapter,
    ProviderCapabilities,
    ProviderCitation,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
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
from app.providers.registry import ProviderRegistry

__all__ = [
    "ProviderAdapter",
    "ProviderAuthenticationError",
    "ProviderBadRequestError",
    "ProviderCapabilities",
    "ProviderCitation",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderModeNotAllowedError",
    "ProviderRateLimitError",
    "ProviderRegistry",
    "ProviderRequest",
    "ProviderResponseError",
    "ProviderResult",
    "ProviderSearchError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "ProviderUsage",
]
