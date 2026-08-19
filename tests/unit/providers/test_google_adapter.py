"""Comprehensive MockTransport tests for the Google Gemini Interactions API adapter.

These tests exercise the GoogleProviderAdapter against an injected
httpx.MockTransport so no real network call is ever made. They cover the
UPDATED Interactions API surface (POST {base_url}/v1/interactions):

- MODEL_ONLY success path (text, usage, model, finish reason, interaction ID)
- Interactions API endpoint used (NOT generateContent)
- store=false sent in the request body
- Exact prompt preservation (no system prompt distortion)
- Configured model used in the request body
- max_output_tokens sent in generation_config (custom + settings default)
- WEB_GROUNDED rejection BEFORE any network call (compliance)
- Configuration errors (missing key / missing model)
- HTTP error mapping (401/429/500)
- Timeout mapping
- Malformed response (empty steps / no model_output)
- Invalid JSON response
- No automatic retry (exactly one HTTP call)
- Capabilities declaration
- repr does not leak the API key
- Thought steps discarded (only model_output text exposed)
- Non-completed status raises ProviderResponseError (failed / incomplete)
- Interaction ID preserved as provider_response_id
"""

from __future__ import annotations

import json
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
    defaults: dict[str, Any] = {
        "app_env": "test",
        "google_api_key": SecretStr("AIza-test-key-12345"),
        "google_scan_model": "gemini-2.5-flash",
        "google_base_url": "https://generativelanguage.googleapis.com",
    }
    defaults.update(overrides)
    return Settings(**defaults)


_INTERACTION_ID = "v1_ChdPU0F4YWFtNkFwS2kxZThQZ05lbXdROBIXT1NBeGFhbTZBcEtpMWU4UGdOZW13UTg"


def _interactions_response(
    *,
    text: str = "Hello! I'm functioning perfectly.",
    status: str = "completed",
    interaction_id: str = _INTERACTION_ID,
    model: str = "gemini-2.5-flash",
    input_tokens: int = 7,
    output_tokens: int = 20,
    total_tokens: int = 49,
    thought_tokens: int = 22,
    cached_tokens: int = 0,
    extra_steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a canonical Google Interactions API response body."""
    steps: list[dict[str, Any]] = [
        {
            "type": "model_output",
            "content": [{"type": "text", "text": text}],
        }
    ]
    if extra_steps:
        steps.extend(extra_steps)
    return {
        "id": interaction_id,
        "object": "interaction",
        "model": model,
        "status": status,
        "steps": steps,
        "usage": {
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "total_thought_tokens": thought_tokens,
            "total_tokens": total_tokens,
            "total_cached_tokens": cached_tokens,
            "total_tool_use_tokens": 0,
        },
    }


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_only_success() -> None:
    """A 200 response maps to a fully-populated ProviderResult."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_interactions_response())
    )
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test prompt", mode=ProviderExecutionMode.MODEL_ONLY)

    result = await adapter.execute(request)

    assert result.response_text == "Hello! I'm functioning perfectly."
    assert result.provider == LLMProvider.GOOGLE
    assert result.surface == ProviderSurface.GOOGLE_INTERACTIONS_API
    assert result.execution_mode == ProviderExecutionMode.MODEL_ONLY
    assert result.provider_response_id == _INTERACTION_ID
    assert result.provider_request_id is None
    assert result.usage.input_tokens == 7
    assert result.usage.output_tokens == 20
    assert result.usage.total_tokens == 49
    assert result.usage.cached_input_tokens == 0
    assert result.usage.reasoning_tokens == 22
    assert result.returned_model == "gemini-2.5-flash"
    assert result.finish_reason == "completed"
    assert result.search_used is False
    assert result.citations == ()


# ---------------------------------------------------------------------------
# Interactions API endpoint used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interactions_api_endpoint_used() -> None:
    """The request URL must end with stable /v1/interactions."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_interactions_response())

    transport = httpx.MockTransport(handler)
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    await adapter.execute(request)

    assert captured["url"].endswith("/v1/interactions")
    assert "/v1beta/" not in captured["url"]
    assert "generateContent" not in captured["url"]


# ---------------------------------------------------------------------------
# store=false sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_false_sent() -> None:
    """The request body must include store=false (stateless one-shot)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json=_interactions_response())

    transport = httpx.MockTransport(handler)
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    await adapter.execute(request)

    assert captured["body"]["store"] is False


# ---------------------------------------------------------------------------
# Exact prompt preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_prompt_preserved() -> None:
    """The prompt text must be sent verbatim in body['input'] (no distortion)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json=_interactions_response())

    transport = httpx.MockTransport(handler)
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    exact_prompt = "Analyze this geo target: São Paulo, Brasil — café & açaí"
    request = ProviderRequest(prompt=exact_prompt, mode=ProviderExecutionMode.MODEL_ONLY)

    await adapter.execute(request)

    assert captured["body"]["input"] == exact_prompt


# ---------------------------------------------------------------------------
# Configured model used
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configured_model_used() -> None:
    """The configured google_scan_model must appear in the request body and result."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json=_interactions_response())

    transport = httpx.MockTransport(handler)
    adapter = GoogleProviderAdapter(
        settings=make_settings(google_scan_model="my-gemini-model"),
        transport=transport,
    )
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    result = await adapter.execute(request)

    assert captured["body"]["model"] == "my-gemini-model"
    assert result.requested_model == "my-gemini-model"


# ---------------------------------------------------------------------------
# max_output_tokens sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_output_tokens_sent() -> None:
    """generation_config.max_output_tokens must reflect the request value when set."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json=_interactions_response())

    transport = httpx.MockTransport(handler)
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(
        prompt="test",
        mode=ProviderExecutionMode.MODEL_ONLY,
        max_output_tokens=2048,
    )

    await adapter.execute(request)

    assert captured["body"]["generation_config"]["max_output_tokens"] == 2048


@pytest.mark.asyncio
async def test_max_output_tokens_settings_default() -> None:
    """When the request omits max_output_tokens, the settings default is used."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode("utf-8"))
        return httpx.Response(200, json=_interactions_response())

    transport = httpx.MockTransport(handler)
    adapter = GoogleProviderAdapter(
        settings=make_settings(provider_max_output_tokens=8192),
        transport=transport,
    )
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    await adapter.execute(request)

    assert captured["body"]["generation_config"]["max_output_tokens"] == 8192


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
    """A 200 response with empty steps (no model_output) maps to ProviderResponseError."""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"id": "x", "status": "completed", "steps": []})
    )
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderResponseError):
        await adapter.execute(request)


# ---------------------------------------------------------------------------
# Invalid JSON response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_json_response() -> None:
    """A 200 response with genuinely invalid JSON maps to ProviderResponseError."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"not json{{{"))
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


# ---------------------------------------------------------------------------
# Thought steps discarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thought_steps_discarded() -> None:
    """Only model_output text must be exposed; thought steps are discarded."""
    thought_text = "Let me reason carefully about this geo target."
    output_text = "The answer is 42."
    body = _interactions_response(
        text=output_text,
        extra_steps=[
            {
                "type": "thought",
                "content": [{"type": "text", "text": thought_text}],
            }
        ],
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    result = await adapter.execute(request)

    assert result.response_text == output_text
    assert thought_text not in result.response_text


# ---------------------------------------------------------------------------
# Non-completed status raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_status_raises_error() -> None:
    """A 200 response with status='failed' maps to ProviderResponseError."""
    body = _interactions_response(status="failed")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderResponseError):
        await adapter.execute(request)


@pytest.mark.asyncio
async def test_incomplete_status_raises_error() -> None:
    """A 200 response with status='incomplete' maps to ProviderResponseError."""
    body = _interactions_response(status="incomplete")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    with pytest.raises(ProviderResponseError):
        await adapter.execute(request)


# ---------------------------------------------------------------------------
# Interaction ID preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interaction_id_preserved() -> None:
    """The response 'id' field must be preserved as provider_response_id."""
    custom_id = "v1_custom-interaction-id-xyz"
    body = _interactions_response(interaction_id=custom_id)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    adapter = GoogleProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)

    result = await adapter.execute(request)

    assert result.provider_response_id == custom_id
