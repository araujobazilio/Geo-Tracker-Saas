"""Comprehensive MockTransport tests for the UPDATED OpenAI Responses API adapter.

These tests inject an ``httpx.MockTransport`` into ``OpenAIProviderAdapter``
so that no real network call is ever made. They cover:
- MODEL_ONLY and WEB_GROUNDED success paths
- WEB_GROUNDED forces tool_choice="required" with web_search as the only tool
  and includes web_search_call.action.sources
- max_output_tokens sent in the request body
- provider_request_id from x-request-id HTTP header
- provider_response_id from response JSON id
- Usage parsing of cached_tokens and reasoning_tokens
- search_requests counted from web_search_call items
- WEB_GROUNDED without observed search -> ProviderSearchError
- Citations parsed from inline url_citation AND web_search_call sources,
  deduplicated by URL
- Malformed JSON -> ProviderResponseError (not JSONDecodeError)
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
    ProviderSearchError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.openai_adapter import OpenAIProviderAdapter

pytestmark = pytest.mark.asyncio


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "app_env": "test",
        "openai_api_key": SecretStr("sk-test-key-12345"),
        "openai_scan_model": "gpt-5.5",
        "openai_base_url": "https://api.openai.com/v1",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def execute_with_transport(
    transport: httpx.AsyncBaseTransport,
    settings: Settings,
    prompt: str = "test prompt",
    mode: ProviderExecutionMode = ProviderExecutionMode.MODEL_ONLY,
    **request_kwargs: Any,
) -> ProviderResult:
    adapter = OpenAIProviderAdapter(settings=settings, transport=transport)
    request = ProviderRequest(prompt=prompt, mode=mode, **request_kwargs)
    return await adapter.execute(request)


def _ok_response(
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    payload = body or {
        "id": "resp_123",
        "model": "gpt-5.5",
        "output_text": "Hello world",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    }
    return httpx.Response(200, json=payload, headers=headers or {})


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
            headers={"x-request-id": "req_abc123"},
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
    assert result.usage.search_requests is None
    assert result.citations == ()
    assert result.provider_request_id == "req_abc123"
    assert result.provider_response_id == "resp_123"


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
            headers={"x-request-id": "req_def456"},
        )

    result = await execute_with_transport(
        make_transport(handler),
        make_settings(),
        mode=ProviderExecutionMode.WEB_GROUNDED,
    )

    assert result.response_text == "Search result answer"
    assert result.search_used is True
    assert result.usage.search_requests == 1
    assert len(result.citations) == 1
    assert result.citations[0].url == "https://example.com"
    assert result.citations[0].title == "Example"
    assert result.provider_request_id == "req_def456"
    assert result.provider_response_id == "resp_456"


# ---------------------------------------------------------------------------
# Request envelope construction
# ---------------------------------------------------------------------------


async def test_web_grounded_forces_tool_choice() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "model": "gpt-5.5",
                "output": [
                    {"type": "web_search_call", "id": "ws_1", "status": "completed"},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Answer", "annotations": []}],
                    },
                ],
                "usage": {},
            },
        )

    await execute_with_transport(
        make_transport(handler),
        make_settings(openai_web_search_max_tool_calls=7),
        mode=ProviderExecutionMode.WEB_GROUNDED,
    )

    assert captured["body"]["tool_choice"] == "required"
    assert captured["body"]["include"] == ["web_search_call.action.sources"]
    assert captured["body"]["tools"] == [{"type": "web_search"}]
    assert captured["body"]["max_tool_calls"] == 7


async def test_max_output_tokens_sent() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ok_response()

    # Custom max_output_tokens in ProviderRequest.
    await execute_with_transport(
        make_transport(handler),
        make_settings(),
        max_output_tokens=2048,
    )
    assert captured["body"]["max_output_tokens"] == 2048

    # Default from settings.
    captured.clear()

    def handler2(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ok_response()

    await execute_with_transport(make_transport(handler2), make_settings())
    assert captured["body"]["max_output_tokens"] == make_settings().provider_max_output_tokens


async def test_store_false_sent() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ok_response()

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
                "usage": {},
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
        return _ok_response()

    prompt = "What are the best CRM tools for small business?"
    await execute_with_transport(make_transport(handler), make_settings(), prompt=prompt)

    assert captured["body"]["input"] == prompt


# ---------------------------------------------------------------------------
# Citation handling
# ---------------------------------------------------------------------------


async def test_web_search_sources_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_src",
                "model": "gpt-5.5",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "ws_1",
                        "status": "completed",
                        "action": {
                            "sources": [
                                {"url": "https://src1.example.com", "title": "Source 1"},
                                {"url": "https://src2.example.com", "title": "Source 2"},
                            ]
                        },
                    },
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Answer", "annotations": []}],
                    },
                ],
                "usage": {},
            },
        )

    result = await execute_with_transport(
        make_transport(handler),
        make_settings(),
        mode=ProviderExecutionMode.WEB_GROUNDED,
    )

    urls = {c.url for c in result.citations}
    assert urls == {"https://src1.example.com", "https://src2.example.com"}
    for cite in result.citations:
        assert cite.source_type == "web_search_source"


async def test_inline_and_source_dedup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_dup",
                "model": "gpt-5.5",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "ws_1",
                        "status": "completed",
                        "action": {
                            "sources": [
                                {"url": "https://example.com"},
                            ]
                        },
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Answer",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com",
                                        "title": "Example",
                                        "start_index": 0,
                                        "end_index": 5,
                                    }
                                ],
                            }
                        ],
                    },
                ],
                "usage": {},
            },
        )

    result = await execute_with_transport(
        make_transport(handler),
        make_settings(),
        mode=ProviderExecutionMode.WEB_GROUNDED,
    )

    assert len(result.citations) == 1
    assert result.citations[0].url == "https://example.com"


async def test_citation_deduplication() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_789",
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
                    },
                ],
                "usage": {},
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
# Usage parsing
# ---------------------------------------------------------------------------


async def test_cached_tokens_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_cached",
                "model": "gpt-5.5",
                "output_text": "Hello",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 50,
                    "total_tokens": 1050,
                    "input_tokens_details": {"cached_tokens": 500},
                },
            },
        )

    result = await execute_with_transport(make_transport(handler), make_settings())

    assert result.usage.cached_input_tokens == 500


async def test_cache_write_tokens_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_cache_write",
                "model": "gpt-5.5",
                "output_text": "Hello",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 50,
                    "total_tokens": 1050,
                    "input_tokens_details": {"cache_write_tokens": 250},
                },
            },
        )

    result = await execute_with_transport(make_transport(handler), make_settings())

    assert result.usage.cache_write_input_tokens == 250


async def test_reasoning_tokens_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_reason",
                "model": "gpt-5.5",
                "output_text": "Hello",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 300,
                    "total_tokens": 310,
                    "output_tokens_details": {"reasoning_tokens": 200},
                },
            },
        )

    result = await execute_with_transport(make_transport(handler), make_settings())

    assert result.usage.reasoning_tokens == 200


async def test_search_requests_counted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_count",
                "model": "gpt-5.5",
                "output": [
                    {"type": "web_search_call", "id": "ws_1", "status": "completed"},
                    {"type": "web_search_call", "id": "ws_2", "status": "completed"},
                    {"type": "web_search_call", "id": "ws_3", "status": "completed"},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Answer", "annotations": []}],
                    },
                ],
                "usage": {},
            },
        )

    result = await execute_with_transport(
        make_transport(handler),
        make_settings(),
        mode=ProviderExecutionMode.WEB_GROUNDED,
    )

    assert result.usage.search_requests == 3
    assert result.search_used is True


# ---------------------------------------------------------------------------
# WEB_GROUNDED search verification
# ---------------------------------------------------------------------------


async def test_web_grounded_without_search_raises_error() -> None:
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(
            200,
            json={
                "id": "resp_nosearch",
                "model": "gpt-5.5",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "No search happened", "annotations": []}
                        ],
                    }
                ],
                "usage": {},
            },
        )

    with pytest.raises(ProviderSearchError):
        await execute_with_transport(
            make_transport(handler),
            make_settings(),
            mode=ProviderExecutionMode.WEB_GROUNDED,
        )

    assert call_count[0] == 1


# ---------------------------------------------------------------------------
# HTTP error mapping
# ---------------------------------------------------------------------------


async def test_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(ProviderAuthenticationError):
        await execute_with_transport(make_transport(handler), make_settings())


async def test_429() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": "rate limited"},
            headers={"Retry-After": "60"},
        )

    with pytest.raises(ProviderRateLimitError) as exc_info:
        await execute_with_transport(make_transport(handler), make_settings())

    assert exc_info.value.retry_after_seconds == 60.0


async def test_500() -> None:
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


async def test_invalid_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"not json at all{{{",
            headers={"content-type": "application/json"},
        )

    transport = httpx.MockTransport(handler)
    adapter = OpenAIProviderAdapter(settings=make_settings(), transport=transport)
    with pytest.raises(ProviderResponseError):
        await adapter.execute(ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY))


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
# Request / response ID parsing
# ---------------------------------------------------------------------------


async def test_x_request_id_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_json_id",
                "output_text": "hi",
                "model": "gpt-5.5",
                "usage": {},
            },
            headers={"x-request-id": "req_header_id"},
        )

    result = await execute_with_transport(make_transport(handler), make_settings())

    assert result.provider_request_id == "req_header_id"
    # provider_request_id must NOT come from the JSON id field.
    assert result.provider_request_id != "resp_json_id"


async def test_response_id_parsed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_json_id",
                "output_text": "hi",
                "model": "gpt-5.5",
                "usage": {},
            },
            headers={"x-request-id": "req_header_id"},
        )

    result = await execute_with_transport(make_transport(handler), make_settings())

    assert result.provider_response_id == "resp_json_id"
