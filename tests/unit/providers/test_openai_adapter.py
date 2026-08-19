"""Comprehensive MockTransport tests for the OpenAI Responses API adapter.

These tests inject an ``httpx.MockTransport`` into ``OpenAIProviderAdapter``
so that no real network call is ever made. They cover:
- MODEL_ONLY and WEB_GROUNDED success paths
- Request envelope construction (store=false, model, prompt, tools)
- Citation parsing and deduplication
- HTTP error mapping (401, 429, 500)
- Transport error mapping (timeout)
- Configuration validation (missing key/model)
- No automatic retry semantics
- Capability and repr safety checks
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.core.enums import LLMProvider, ProviderExecutionMode, ProviderSurface
from app.providers.base import ProviderRequest, ProviderResult
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.openai_adapter import OpenAIProviderAdapter

pytestmark = pytest.mark.asyncio


def make_settings(**overrides: Any) -> Settings:
    defaults = {
        "app_env": "test",
        "openai_api_key": SecretStr("sk-test-key-12345"),
        "openai_scan_model": "gpt-5.5",
        "openai_base_url": "https://api.openai.com/v1",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def execute_with_transport(
    transport: httpx.AsyncBaseTransport,
    settings: Settings,
    prompt: str = "test prompt",
    mode: ProviderExecutionMode = ProviderExecutionMode.MODEL_ONLY,
) -> ProviderResult:
    adapter = OpenAIProviderAdapter(settings=settings, transport=transport)
    request = ProviderRequest(prompt=prompt, mode=mode)
    return await adapter.execute(request)


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------


async def test_model_only_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_123",
                "model": "gpt-5.5",
                "output_text": "Hello world",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    result = await execute_with_transport(make_transport(handler), make_settings())

    assert result.response_text == "Hello world"
    assert result.provider == LLMProvider.OPENAI
    assert result.surface == ProviderSurface.OPENAI_RESPONSES_API
    assert result.execution_mode == ProviderExecutionMode.MODEL_ONLY
    assert result.requested_model == "gpt-5.5"
    assert result.returned_model == "gpt-5.5"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.usage.total_tokens == 15
    assert result.search_used is False
    assert result.citations == ()


async def test_web_grounded_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_456",
                "model": "gpt-5.5",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "ws_1",
                        "status": "completed",
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Search result answer",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com",
                                        "title": "Example",
                                        "start_index": 0,
                                        "end_index": 10,
                                    }
                                ],
                            }
                        ],
                    },
                ],
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )

    result = await execute_with_transport(
        make_transport(handler),
        make_settings(),
        mode=ProviderExecutionMode.WEB_GROUNDED,
    )

    assert result.response_text == "Search result answer"
    assert result.search_used is True
    assert len(result.citations) == 1
    assert result.citations[0].url == "https://example.com"
    assert result.citations[0].title == "Example"


# ---------------------------------------------------------------------------
# Request envelope construction
# ---------------------------------------------------------------------------


async def test_store_false_sent() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "gpt-5.5",
                "output_text": "Hello",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    await execute_with_transport(make_transport(handler), make_settings())

    assert captured["body"]["store"] is False


async def test_configured_model_used() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "my-custom-model",
                "output_text": "Hello",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    settings = make_settings(openai_scan_model="my-custom-model")
    result = await execute_with_transport(make_transport(handler), settings)

    assert captured["body"]["model"] == "my-custom-model"
    assert result.requested_model == "my-custom-model"


async def test_exact_prompt_preserved() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "gpt-5.5",
                "output_text": "Hello",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    prompt = "What are the best CRM tools for small business?"
    await execute_with_transport(make_transport(handler), make_settings(), prompt=prompt)

    assert captured["body"]["input"] == prompt


# ---------------------------------------------------------------------------
# Citation handling
# ---------------------------------------------------------------------------


async def test_citation_deduplication() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_789",
                "model": "gpt-5.5",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Answer with citations",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com",
                                        "title": "Example",
                                        "start_index": 0,
                                        "end_index": 5,
                                    },
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com",
                                        "title": "Example Duplicate",
                                        "start_index": 6,
                                        "end_index": 10,
                                    },
                                ],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    result = await execute_with_transport(
        make_transport(handler),
        make_settings(),
        mode=ProviderExecutionMode.WEB_GROUNDED,
    )

    assert len(result.citations) == 1
    assert result.citations[0].url == "https://example.com"


# ---------------------------------------------------------------------------
# HTTP error mapping
# ---------------------------------------------------------------------------


async def test_401_authentication_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(ProviderAuthenticationError):
        await execute_with_transport(make_transport(handler), make_settings())


async def test_429_rate_limit_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": "rate limited"},
            headers={"Retry-After": "60"},
        )

    with pytest.raises(ProviderRateLimitError) as exc_info:
        await execute_with_transport(make_transport(handler), make_settings())

    assert exc_info.value.retry_after_seconds == 60.0


async def test_500_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    with pytest.raises(ProviderUnavailableError):
        await execute_with_transport(make_transport(handler), make_settings())


# ---------------------------------------------------------------------------
# Transport error mapping
# ---------------------------------------------------------------------------


async def test_timeout() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    with pytest.raises(ProviderTimeoutError):
        await execute_with_transport(make_transport(timeout_handler), make_settings())


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


async def test_malformed_200_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "resp", "model": "gpt-5.5"})

    with pytest.raises(ProviderResponseError):
        await execute_with_transport(make_transport(handler), make_settings())


# ---------------------------------------------------------------------------
# Retry semantics
# ---------------------------------------------------------------------------


async def test_no_automatic_retry() -> None:
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(500, json={"error": "server error"})

    with pytest.raises(ProviderUnavailableError):
        await execute_with_transport(make_transport(handler), make_settings())

    assert call_count[0] == 1


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


async def test_missing_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "resp", "output_text": "hi", "model": "gpt-5.5"})

    settings = make_settings(openai_api_key=SecretStr(""))

    with pytest.raises(ProviderConfigurationError):
        await execute_with_transport(make_transport(handler), settings)


async def test_missing_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "resp", "output_text": "hi", "model": "gpt-5.5"})

    settings = make_settings(openai_scan_model="")

    with pytest.raises(ProviderConfigurationError):
        await execute_with_transport(make_transport(handler), settings)


# ---------------------------------------------------------------------------
# Capabilities and repr safety
# ---------------------------------------------------------------------------


async def test_capabilities() -> None:
    adapter = OpenAIProviderAdapter(settings=make_settings())
    caps = adapter.capabilities()

    assert caps.supports_model_only is True
    assert caps.supports_web_grounded is True
    assert caps.supports_citations is True
    assert caps.supports_search_result_metadata is True


async def test_repr_no_api_key() -> None:
    adapter = OpenAIProviderAdapter(settings=make_settings())

    assert "sk-test-key-12345" not in repr(adapter)


# ---------------------------------------------------------------------------
# Request ID parsing
# ---------------------------------------------------------------------------


async def test_request_id_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "resp_abc123", "output_text": "hi", "model": "gpt-5.5", "usage": {}},
        )

    result = await execute_with_transport(make_transport(handler), make_settings())

    assert result.provider_request_id == "resp_abc123"
