"""Comprehensive MockTransport tests for the Google Gemini adapter.

These tests exercise the GoogleProviderAdapter against an injected
httpx.MockTransport so no real network call is ever made. They cover:
- MODEL_ONLY success path (text, usage, model, finish reason)
- Exact prompt preservation (no system prompt distortion)
- Configured model used in URL
- WEB_GROUNDED rejection BEFORE any network call (compliance)
- Configuration errors (missing key / missing model)
- HTTP error mapping (401/403/429/500)
- Timeout mapping
- Malformed response (empty candidates)
- No automatic retry (exactly one HTTP call)
- Capabilities declaration
- repr does not leak the API key
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.core.enums import LLMProvider, ProviderExecutionMode, ProviderSurface
from app.providers.base import ProviderRequest
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderModeNotAllowedError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.google_adapter import GoogleProviderAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_settings(**overrides: Any) -> Settings:
    """Build a Settings instance with sane Google defaults for tests."""
    defaults = {
        "app_env": "test",
        "google_api_key": SecretStr("AIza-test-key-12345"),
        "google_scan_model": "gemini-2.5-flash",
        "google_base_url": "https://generativelanguage.googleapis.com",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _gemini_response(
    *,
    text: str = "Hello from Gemini",
    prompt_tokens: int = 5,
    candidates_tokens: int = 10,
    total_tokens: int = 15,
    model_version: str = "gemini-2.5-flash-001",
    finish_reason: str = "STOP",
) -> dict:
    """Build a canonical Gemini generateContent response body."""
    return {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}],
                    "role": "model",
                },
                "finishReason": finish_reason,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": candidates_tokens,
            "totalTokenCount": total_tokens,
        },
        "modelVersion": model_version,
    }


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_only_success() -> None:
    """A 200 response maps to a fully-populated ProviderResult."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_gemini_response()))
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test prompt", mode=ProviderExecutionMode.MODEL_ONLY)

    result = await adapter.execute(request)

    assert result.response_text == "Hello from Gemini"
    assert result.provider == LLMProvider.GOOGLE
    assert result.surface == ProviderSurface.GOOGLE_GEMINI_API
    assert result.execution_mode == ProviderExecutionMode.MODEL_ONLY
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 10
    assert result.usage.total_tokens == 15
    assert result.returned_model == "gemini-2.5-flash-001"
    assert result.finish_reason == "STOP"
    assert result.search_used is False
    assert result.citations == ()
    assert result.provider_request_id is None


# ---------------------------------------------------------------------------
# Prompt preservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_prompt_preserved() -> None:
    """The prompt text must be sent verbatim with no system prompt distortion."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json=_gemini_response())

    transport = httpx.MockTransport(handler)
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    exact_prompt = "Analyze this geo target: São Paulo, Brasil — café & açaí"
    request = ProviderRequest(prompt=exact_prompt, mode=ProviderExecutionMode.MODEL_ONLY)

    await adapter.execute(request)

    import json

    body = json.loads(captured["body"])
    assert body["contents"][0]["parts"][0]["text"] == exact_prompt


# ---------------------------------------------------------------------------
# Configured model used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configured_model_used() -> None:
    """The configured google_scan_model must appear in the URL path."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_gemini_response())

    transport = httpx.MockTransport(handler)
    adapter = GoogleProviderAdapter(
        settings=make_settings(google_scan_model="my-gemini-model"),
        transport=transport,
    )
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    result = await adapter.execute(request)

    assert "models/my-gemini-model:generateContent" in captured["url"]
    assert result.requested_model == "my-gemini-model"


# ---------------------------------------------------------------------------
# Usage parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_parsing() -> None:
    """usageMetadata fields map to ProviderUsage fields."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=_gemini_response(
                prompt_tokens=42,
                candidates_tokens=99,
                total_tokens=141,
            ),
        )
    )
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    result = await adapter.execute(request)

    assert result.usage.input_tokens == 42
    assert result.usage.output_tokens == 99
    assert result.usage.total_tokens == 141


# ---------------------------------------------------------------------------
# Returned model parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returned_model_parsing() -> None:
    """modelVersion maps to returned_model."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=_gemini_response(model_version="gemini-2.5-flash-002"),
        )
    )
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    result = await adapter.execute(request)

    assert result.returned_model == "gemini-2.5-flash-002"


# ---------------------------------------------------------------------------
# WEB_GROUNDED rejected before network (compliance-critical)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_grounded_rejected_before_network() -> None:
    """WEB_GROUNDED must raise BEFORE any HTTP call (compliance restriction)."""
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handler)
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.WEB_GROUNDED)

    with pytest.raises(ProviderModeNotAllowedError):
        await adapter.execute(request)

    assert call_count[0] == 0, "Google WEB_GROUNDED must not make any network call"


# ---------------------------------------------------------------------------
# Configuration errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_api_key() -> None:
    """An empty API key must raise ProviderConfigurationError."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    adapter = GoogleProviderAdapter(
        settings=make_settings(google_api_key=SecretStr("")),
        transport=transport,
    )
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderConfigurationError):
        await adapter.execute(request)


@pytest.mark.asyncio
async def test_missing_model() -> None:
    """An empty scan model must raise ProviderConfigurationError."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    adapter = GoogleProviderAdapter(
        settings=make_settings(google_scan_model=""),
        transport=transport,
    )
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderConfigurationError):
        await adapter.execute(request)


# ---------------------------------------------------------------------------
# HTTP error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401() -> None:
    """A 401 response maps to ProviderAuthenticationError."""
    transport = httpx.MockTransport(lambda request: httpx.Response(401, json={}))
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderAuthenticationError):
        await adapter.execute(request)


@pytest.mark.asyncio
async def test_403() -> None:
    """A 403 response maps to ProviderAuthenticationError."""
    transport = httpx.MockTransport(lambda request: httpx.Response(403, json={}))
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderAuthenticationError):
        await adapter.execute(request)


@pytest.mark.asyncio
async def test_429() -> None:
    """A 429 response with Retry-After maps to ProviderRateLimitError."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            429,
            headers={"Retry-After": "45"},
            json={},
        )
    )
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderRateLimitError) as exc_info:
        await adapter.execute(request)

    assert exc_info.value.retry_after_seconds == 45.0


@pytest.mark.asyncio
async def test_500() -> None:
    """A 500 response maps to ProviderUnavailableError."""
    transport = httpx.MockTransport(lambda request: httpx.Response(500, json={}))
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderUnavailableError):
        await adapter.execute(request)


# ---------------------------------------------------------------------------
# Timeout mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout() -> None:
    """A transport TimeoutException maps to ProviderTimeoutError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout")

    transport = httpx.MockTransport(handler)
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderTimeoutError):
        await adapter.execute(request)


# ---------------------------------------------------------------------------
# Malformed response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_body() -> None:
    """A 200 response with no candidates maps to ProviderResponseError."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"candidates": []}))
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderResponseError):
        await adapter.execute(request)


# ---------------------------------------------------------------------------
# No automatic retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_automatic_retry() -> None:
    """The adapter must make exactly ONE HTTP call even on a 500 error."""
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(500, json={})

    transport = httpx.MockTransport(handler)
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderUnavailableError):
        await adapter.execute(request)

    assert call_count[0] == 1, "Google adapter must not retry automatically"


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def test_capabilities() -> None:
    """capabilities() declares MODEL_ONLY-only, no citations, no web grounding."""
    adapter = GoogleProviderAdapter(settings=make_settings())
    caps = adapter.capabilities()

    assert caps.supports_model_only is True
    assert caps.supports_web_grounded is False
    assert caps.supports_citations is False


# ---------------------------------------------------------------------------
# repr does not leak the API key
# ---------------------------------------------------------------------------


def test_repr_no_api_key() -> None:
    """repr must not contain the secret API key value."""
    adapter = GoogleProviderAdapter(settings=make_settings())
    representation = repr(adapter)

    assert "AIza-test-key-12345" not in representation
