"""Shared HTTP utilities for provider adapters.

Centralizes:
- httpx.AsyncClient construction with configured timeouts
- HTTP error mapping (status code → domain exception)
- Monotonic latency measurement
- Structured logging (sanitized — no keys, headers, or response bodies)
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.enums import LLMProvider
from app.core.logging import get_logger
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderBadRequestError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

logger = get_logger("app.providers")


def build_async_client(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient with configured timeouts.

    An optional transport can be injected for testing (MockTransport).
    """
    s = settings or get_settings()
    timeout = httpx.Timeout(
        connect=s.provider_connect_timeout_seconds,
        read=s.provider_read_timeout_seconds,
        write=s.provider_connect_timeout_seconds,
        pool=s.provider_connect_timeout_seconds,
    )
    kwargs: dict[str, Any] = {"timeout": timeout}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


def map_http_error(
    provider: LLMProvider,
    provider_name: str,
    response: httpx.Response,
) -> None:
    """Map an HTTP error response to a domain exception.

    Raises the appropriate ProviderError subclass.
    Does NOT include the response body in the error message.
    """
    status = response.status_code
    provider_str = provider.value

    if status == 400:
        raise ProviderBadRequestError(
            f"{provider_name} rejected the request (400).",
            provider=provider_str,
        )
    if status in (401, 403):
        raise ProviderAuthenticationError(
            f"{provider_name} authentication failed ({status}).",
            provider=provider_str,
        )
    if status == 429:
        retry_after = _parse_retry_after(response)
        raise ProviderRateLimitError(
            f"{provider_name} rate-limited the request (429).",
            provider=provider_str,
            retry_after_seconds=retry_after,
        )
    if 500 <= status < 600:
        raise ProviderUnavailableError(
            f"{provider_name} returned a server error ({status}).",
            provider=provider_str,
        )
    # Other unexpected status codes.
    raise ProviderResponseError(
        f"{provider_name} returned unexpected status {status}.",
        provider=provider_str,
    )


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Safently parse Retry-After header (seconds or HTTP date)."""
    value = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        # Could be an HTTP-date; we don't parse those for safety.
        return None


def map_transport_error(
    provider: LLMProvider,
    provider_name: str,
    exc: Exception,
) -> Exception:
    """Map an httpx transport exception to a domain exception."""
    provider_str = provider.value
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeoutError(
            f"{provider_name} request timed out.",
            provider=provider_str,
        )
    if isinstance(exc, httpx.ConnectError):
        return ProviderUnavailableError(
            f"{provider_name} connection failed.",
            provider=provider_str,
        )
    # For other httpx or network errors, wrap as unavailable.
    return ProviderUnavailableError(
        f"{provider_name} transport error: {type(exc).__name__}.",
        provider=provider_str,
    )


def parse_json_response(
    provider: LLMProvider,
    provider_name: str,
    response: httpx.Response,
) -> dict[str, Any]:
    """Safely parse an HTTP response body as JSON.

    A nominal HTTP 2xx response with invalid JSON must result in
    ProviderResponseError — never leak JSONDecodeError, ValueError,
    or httpx internals.

    Returns the parsed dict on success.
    """
    provider_str = provider.value
    try:
        data = response.json()
    except Exception as exc:
        raise ProviderResponseError(
            f"{provider_name} returned an unparseable response body.",
            provider=provider_str,
        ) from exc
    if not isinstance(data, dict):
        raise ProviderResponseError(
            f"{provider_name} returned a non-object JSON response.",
            provider=provider_str,
        )
    return data


class LatencyTimer:
    """Monotonic latency measurement for provider requests.

    Uses time.monotonic() to avoid wall-clock issues.
    """

    def __init__(self) -> None:
        self._start: float = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)


def log_provider_result(
    provider: LLMProvider,
    surface: str,
    execution_mode: str,
    requested_model: str,
    returned_model: str | None,
    provider_request_id: str | None,
    status: str,
    latency_ms: int,
    usage_input_tokens: int | None,
    usage_output_tokens: int | None,
    search_requests: int | None,
    correlation_id: str | None = None,
    provider_response_id: str | None = None,
) -> None:
    """Log a sanitized structured provider result.

    NEVER logs: API keys, Authorization headers, full request headers,
    full response body, or full Prompt.text.
    """
    logger.info(
        "provider_request_completed",
        provider=provider.value,
        surface=surface,
        execution_mode=execution_mode,
        requested_model=requested_model,
        returned_model=returned_model,
        provider_request_id=provider_request_id,
        provider_response_id=provider_response_id,
        status=status,
        latency_ms=latency_ms,
        usage_input_tokens=usage_input_tokens,
        usage_output_tokens=usage_output_tokens,
        search_requests=search_requests,
        correlation_id=correlation_id,
    )
