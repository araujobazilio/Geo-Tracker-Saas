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
from app.providers.base import ProviderFailureEvidence, ProviderRequest, ProviderResult
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderContractViolationError,
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


# ---------------------------------------------------------------------------
# Phase 13.5.10 — Provider failure evidence (case B)
#
# Responses that reached the provider and returned a valid envelope with
# usage/IDs but are NOT functionally usable (empty text, incomplete status,
# max_tool_calls violation) must carry ProviderFailureEvidence on the raised
# ProviderResponseError / ProviderSearchError.
# ---------------------------------------------------------------------------


async def test_incomplete_response_carries_evidence() -> None:
    """status=incomplete, incomplete_details.reason=max_output_tokens,
    empty output_text, usage present -> ProviderResponseError with evidence."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_incomplete",
                "model": "gpt-5.5",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
                "output_text": "",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 0,
                    "total_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
            headers={"x-request-id": "req_incomplete_001"},
        )

    with pytest.raises(ProviderResponseError) as exc_info:
        await execute_with_transport(make_transport(handler), make_settings())

    err = exc_info.value
    assert err.evidence is not None
    ev = err.evidence
    assert isinstance(ev, ProviderFailureEvidence)
    assert ev.provider == LLMProvider.OPENAI
    assert ev.surface == ProviderSurface.OPENAI_RESPONSES_API
    assert ev.execution_mode == ProviderExecutionMode.MODEL_ONLY
    assert ev.requested_model == "gpt-5.5"
    assert ev.returned_model == "gpt-5.5"
    assert ev.provider_request_id == "req_incomplete_001"
    assert ev.provider_response_id == "resp_incomplete"
    assert ev.usage.input_tokens == 100
    assert ev.usage.output_tokens == 0
    assert ev.usage.cached_input_tokens == 20
    assert ev.usage.reasoning_tokens == 0
    assert ev.incomplete_reason == "max_output_tokens"
    assert ev.max_tool_calls_violation is None
    assert ev.latency_ms >= 0


async def test_incomplete_response_with_partial_text_is_not_success() -> None:
    """Incomplete provider output remains inconclusive even with text."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_partial_incomplete",
                "model": "gpt-5.5",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output_text": "partial answer",
                "output": [
                    {"type": "web_search_call", "action": {"type": "search"}},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

    with pytest.raises(ProviderResponseError) as exc_info:
        await execute_with_transport(
            make_transport(handler), make_settings(), mode=ProviderExecutionMode.WEB_GROUNDED
        )

    assert exc_info.value.evidence is not None
    assert exc_info.value.evidence.incomplete_reason == "max_output_tokens"


async def test_empty_output_with_usage_carries_evidence() -> None:
    """output_text empty (no incomplete_details), usage/IDs present ->
    ProviderResponseError with evidence preserving billable material."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_empty",
                "model": "gpt-5.5",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": ""}],
                    }
                ],
                "output_text": "",
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 0,
                    "total_tokens": 50,
                },
            },
            headers={"x-request-id": "req_empty_002"},
        )

    with pytest.raises(ProviderResponseError) as exc_info:
        await execute_with_transport(make_transport(handler), make_settings())

    ev = exc_info.value.evidence
    assert ev is not None
    assert ev.provider_request_id == "req_empty_002"
    assert ev.provider_response_id == "resp_empty"
    assert ev.usage.input_tokens == 50
    assert ev.usage.output_tokens == 0
    assert ev.incomplete_reason is None
    assert ev.max_tool_calls_violation is None


async def test_web_grounded_missing_search_carries_evidence() -> None:
    """WEB_GROUNDED with no web_search_call but valid envelope ->
    ProviderSearchError with evidence."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_nosearch_ev",
                "model": "gpt-5.5",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "No search result", "annotations": []}
                        ],
                    }
                ],
                "usage": {"input_tokens": 30, "output_tokens": 5, "total_tokens": 35},
            },
            headers={"x-request-id": "req_nosearch_ev"},
        )

    with pytest.raises(ProviderSearchError) as exc_info:
        await execute_with_transport(
            make_transport(handler),
            make_settings(),
            mode=ProviderExecutionMode.WEB_GROUNDED,
        )

    ev = exc_info.value.evidence
    assert ev is not None
    assert ev.provider_request_id == "req_nosearch_ev"
    assert ev.provider_response_id == "resp_nosearch_ev"
    assert ev.usage.input_tokens == 30
    assert ev.search_used is False


async def test_max_tool_calls_violation_detected() -> None:
    """WEB_GROUNDED with search_requests=2 but max_tool_calls=1 ->
    ProviderContractViolationError even when response_text is valid.
    The count is NOT clamped.  Evidence carries requested and observed."""

    settings = make_settings(openai_web_search_max_tool_calls=1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_violation",
                "model": "gpt-5.5",
                "output": [
                    {"type": "web_search_call", "id": "ws_1", "status": "completed"},
                    {"type": "web_search_call", "id": "ws_2", "status": "completed"},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Result with 2 searches"}],
                    },
                ],
                "usage": {
                    "input_tokens": 40,
                    "output_tokens": 10,
                    "total_tokens": 50,
                },
            },
            headers={"x-request-id": "req_violation"},
        )

    with pytest.raises(ProviderContractViolationError) as exc_info:
        await execute_with_transport(
            make_transport(handler),
            settings,
            mode=ProviderExecutionMode.WEB_GROUNDED,
        )

    err = exc_info.value
    assert err.evidence is not None
    ev = err.evidence
    # Count is preserved, NOT clamped.
    assert ev.usage.search_requests == 2
    assert ev.observed_search_requests == 2
    # Requested limit is snapshotted for historical auditability.
    assert ev.requested_max_tool_calls == 1
    assert ev.max_tool_calls_violation == 2
    # IDs preserved.
    assert ev.provider_request_id == "req_violation"
    assert ev.provider_response_id == "resp_violation"
    # Usage preserved.
    assert ev.usage.input_tokens == 40
    assert ev.usage.output_tokens == 10
    # Error message is deterministic and contains both values.
    assert "requested=1" in err.message
    assert "observed=2" in err.message


async def test_max_tool_calls_violation_with_empty_text_carries_evidence() -> None:
    """WEB_GROUNDED with search_requests=2 (max=1) AND empty text ->
    ProviderResponseError with evidence including max_tool_calls_violation."""

    settings = make_settings(openai_web_search_max_tool_calls=1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_violation_empty",
                "model": "gpt-5.5",
                "output": [
                    {"type": "web_search_call", "id": "ws_1", "status": "completed"},
                    {"type": "web_search_call", "id": "ws_2", "status": "completed"},
                ],
                "output_text": "",
                "usage": {
                    "input_tokens": 40,
                    "output_tokens": 0,
                    "total_tokens": 40,
                },
            },
            headers={"x-request-id": "req_violation_empty"},
        )

    with pytest.raises(ProviderResponseError) as exc_info:
        await execute_with_transport(
            make_transport(handler),
            settings,
            mode=ProviderExecutionMode.WEB_GROUNDED,
        )

    ev = exc_info.value.evidence
    assert ev is not None
    assert ev.usage.search_requests == 2
    assert ev.max_tool_calls_violation == 2
    assert ev.requested_max_tool_calls == 1
    assert ev.observed_search_requests == 2
    # Count is NOT clamped.
    assert ev.usage.search_requests != 1


async def test_incomplete_status_without_details_gets_generic_reason() -> None:
    """status=incomplete but no incomplete_details -> incomplete_reason='incomplete'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_inc_no_details",
                "model": "gpt-5.5",
                "status": "incomplete",
                "output_text": "",
                "usage": {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10},
            },
            headers={"x-request-id": "req_inc_no_details"},
        )

    with pytest.raises(ProviderResponseError) as exc_info:
        await execute_with_transport(make_transport(handler), make_settings())

    ev = exc_info.value.evidence
    assert ev is not None
    assert ev.incomplete_reason == "incomplete"


async def test_pre_provider_error_has_no_evidence() -> None:
    """401 error (case A — before any billable response) -> no evidence."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(ProviderAuthenticationError) as exc_info:
        await execute_with_transport(make_transport(handler), make_settings())

    assert exc_info.value.evidence is None


async def test_timeout_error_has_no_evidence() -> None:
    """Timeout (case A) -> no evidence."""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    with pytest.raises(ProviderTimeoutError) as exc_info:
        await execute_with_transport(make_transport(timeout_handler), make_settings())

    assert exc_info.value.evidence is None


async def test_evidence_does_not_contain_raw_body() -> None:
    """ProviderFailureEvidence must not expose raw response body or API key.
    It only carries structured fields."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_safe",
                "model": "gpt-5.5",
                "output_text": "",
                "usage": {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
                # Hypothetical sensitive field that should NOT leak
                "secret_internal": "should-not-leak",
            },
            headers={"x-request-id": "req_safe"},
        )

    with pytest.raises(ProviderResponseError) as exc_info:
        await execute_with_transport(make_transport(handler), make_settings())

    ev = exc_info.value.evidence
    assert ev is not None
    # Evidence is a frozen dataclass with only typed fields — no raw body.
    assert not hasattr(ev, "raw_body")
    assert not hasattr(ev, "response_body")
    # Check it doesn't have a metadata/dict field that could leak.
    assert not hasattr(ev, "metadata")


# ---------------------------------------------------------------------------
# Web tool evidence & billing split (Phase 13.5.12G)
#
# Every ``web_search_call`` output item is one built-in tool call processed by
# OpenAI and counts toward ``max_tool_calls`` (bound authority =
# web_tool_call_count).  Only ``action.type == "search"`` is a documented
# billable web-search call (billing authority = search_action_count).
# Legacy ``search_requests`` == web_tool_call_count for compatibility.
# ---------------------------------------------------------------------------


def _ws(item_id: str, action_type: str | None) -> dict[str, Any]:
    item: dict[str, Any] = {"type": "web_search_call", "id": item_id, "status": "completed"}
    if action_type is not None:
        item["action"] = {"type": action_type}
    return item


def _web_response(*items: dict[str, Any], text: str = "grounded answer") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_web",
            "model": "gpt-5.5",
            "output": [
                *items,
                {"type": "message", "content": [{"type": "output_text", "text": text}]},
            ],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        },
        headers={"x-request-id": "req_web"},
    )


async def test_two_search_actions_within_bound() -> None:
    """2 search, max=2 ? total=2, search=2, legacy=2, no violation."""
    settings = make_settings(openai_web_search_max_tool_calls=2)
    resp = _web_response(_ws("ws_1", "search"), _ws("ws_2", "search"))

    result = await execute_with_transport(
        make_transport(lambda r: resp), settings, mode=ProviderExecutionMode.WEB_GROUNDED
    )

    u = result.usage
    assert u.web_tool_call_count == 2
    assert u.search_action_count == 2
    assert u.open_page_action_count == 0
    assert u.find_in_page_action_count == 0
    assert u.unknown_web_action_count == 0
    assert u.search_requests == 2  # legacy == total
    assert result.search_used is True


async def test_one_search_one_open_page_within_bound() -> None:
    """1 search + 1 open_page, max=2 ? total=2 (no violation), billable search=1."""
    settings = make_settings(openai_web_search_max_tool_calls=2)
    resp = _web_response(_ws("ws_1", "search"), _ws("ws_2", "open_page"))

    result = await execute_with_transport(
        make_transport(lambda r: resp), settings, mode=ProviderExecutionMode.WEB_GROUNDED
    )

    u = result.usage
    assert u.web_tool_call_count == 2
    assert u.search_action_count == 1
    assert u.open_page_action_count == 1
    assert u.find_in_page_action_count == 0
    assert u.unknown_web_action_count == 0
    assert u.search_requests == 2  # legacy is TOTAL, not billable count


async def test_search_open_find_exceeds_bound_even_with_one_billable_search() -> None:
    """1 search + 1 open_page + 1 find_in_page, max=2 ? total=3 ? violation.
    Billable search component remains 1."""
    settings = make_settings(openai_web_search_max_tool_calls=2)
    resp = _web_response(
        _ws("ws_1", "search"), _ws("ws_2", "open_page"), _ws("ws_3", "find_in_page")
    )

    with pytest.raises(ProviderContractViolationError) as exc_info:
        await execute_with_transport(
            make_transport(lambda r: resp), settings, mode=ProviderExecutionMode.WEB_GROUNDED
        )

    ev = exc_info.value.evidence
    assert ev is not None
    assert ev.requested_max_tool_calls == 2
    assert ev.observed_web_tool_call_count == 3
    assert ev.observed_search_requests == 3  # legacy == total
    assert ev.max_tool_calls_violation == 3
    assert ev.search_action_count == 1
    assert ev.open_page_action_count == 1
    assert ev.find_in_page_action_count == 1
    assert ev.unknown_web_action_count == 0
    assert ev.usage.search_action_count == 1
    assert "requested=2" in exc_info.value.message
    assert "observed=3" in exc_info.value.message


async def test_two_search_one_open_page_exceeds_bound() -> None:
    """2 search + 1 open_page, max=2 ? total=3 ? violation; billable search=2."""
    settings = make_settings(openai_web_search_max_tool_calls=2)
    resp = _web_response(_ws("ws_1", "search"), _ws("ws_2", "search"), _ws("ws_3", "open_page"))

    with pytest.raises(ProviderContractViolationError) as exc_info:
        await execute_with_transport(
            make_transport(lambda r: resp), settings, mode=ProviderExecutionMode.WEB_GROUNDED
        )

    ev = exc_info.value.evidence
    assert ev is not None
    assert ev.observed_web_tool_call_count == 3
    assert ev.search_action_count == 2
    assert ev.open_page_action_count == 1
    assert ev.max_tool_calls_violation == 3


async def test_three_search_actions_exceed_bound() -> None:
    """3 search, max=2 ? total=3 ? violation; billable search=3."""
    settings = make_settings(openai_web_search_max_tool_calls=2)
    resp = _web_response(_ws("ws_1", "search"), _ws("ws_2", "search"), _ws("ws_3", "search"))

    with pytest.raises(ProviderContractViolationError) as exc_info:
        await execute_with_transport(
            make_transport(lambda r: resp), settings, mode=ProviderExecutionMode.WEB_GROUNDED
        )

    ev = exc_info.value.evidence
    assert ev is not None
    assert ev.observed_web_tool_call_count == 3
    assert ev.search_action_count == 3
    assert ev.open_page_action_count == 0
    assert ev.max_tool_calls_violation == 3


async def test_unknown_action_type_counts_toward_bound_not_billing() -> None:
    """unknown action + 1 search, max=2 ? total=2 (within bound),
    search=1, unknown=1.  The unknown item is never assumed billable."""
    settings = make_settings(openai_web_search_max_tool_calls=2)
    resp = _web_response(_ws("ws_1", "search"), _ws("ws_2", "some_future_action"))

    result = await execute_with_transport(
        make_transport(lambda r: resp), settings, mode=ProviderExecutionMode.WEB_GROUNDED
    )

    u = result.usage
    assert u.web_tool_call_count == 2
    assert u.search_action_count == 1
    assert u.open_page_action_count == 0
    assert u.find_in_page_action_count == 0
    assert u.unknown_web_action_count == 1
    assert u.search_requests == 2


async def test_missing_action_dict_is_classified_unknown() -> None:
    """A web_search_call item without an action dict ? unknown."""
    settings = make_settings(openai_web_search_max_tool_calls=2)
    resp = _web_response(_ws("ws_1", None), _ws("ws_2", "search"))

    result = await execute_with_transport(
        make_transport(lambda r: resp), settings, mode=ProviderExecutionMode.WEB_GROUNDED
    )

    u = result.usage
    assert u.web_tool_call_count == 2
    assert u.search_action_count == 1
    assert u.unknown_web_action_count == 1


async def test_non_dict_action_is_classified_unknown() -> None:
    """A web_search_call item whose action is not a dict ? unknown."""
    settings = make_settings(openai_web_search_max_tool_calls=2)
    item = {"type": "web_search_call", "id": "ws_1", "status": "completed", "action": "search"}
    resp = _web_response(item)

    result = await execute_with_transport(
        make_transport(lambda r: resp), settings, mode=ProviderExecutionMode.WEB_GROUNDED
    )

    assert result.usage.web_tool_call_count == 1
    assert result.usage.search_action_count == 0
    assert result.usage.unknown_web_action_count == 1


async def test_counter_invariant_holds_for_mixed_actions() -> None:
    """total == search + open_page + find_in_page + unknown."""
    settings = make_settings(openai_web_search_max_tool_calls=10)
    resp = _web_response(
        _ws("a", "search"),
        _ws("b", "search"),
        _ws("c", "open_page"),
        _ws("d", "find_in_page"),
        _ws("e", "find_in_page"),
        _ws("f", None),
    )

    result = await execute_with_transport(
        make_transport(lambda r: resp), settings, mode=ProviderExecutionMode.WEB_GROUNDED
    )

    u = result.usage
    assert u.web_tool_call_count == 6
    assert u.search_action_count == 2
    assert u.open_page_action_count == 1
    assert u.find_in_page_action_count == 2
    assert u.unknown_web_action_count == 1
    assert u.web_tool_call_count == (
        u.search_action_count
        + u.open_page_action_count
        + u.find_in_page_action_count
        + u.unknown_web_action_count
    )


async def test_model_only_has_no_web_tool_counters() -> None:
    """MODEL_ONLY: no web_search_call ? all web counters None, legacy None."""
    result = await execute_with_transport(make_transport(lambda r: _ok_response()), make_settings())

    u = result.usage
    assert u.web_tool_call_count is None
    assert u.search_action_count is None
    assert u.open_page_action_count is None
    assert u.find_in_page_action_count is None
    assert u.unknown_web_action_count is None
    assert u.search_requests is None


async def test_web_grounded_no_web_search_call_raises_search_error_with_none_counters() -> None:
    """WEB_GROUNDED without any web_search_call ? ProviderSearchError; counters None."""
    settings = make_settings(openai_web_search_max_tool_calls=2)
    resp = _web_response()  # no web_search_call items

    with pytest.raises(ProviderSearchError) as exc_info:
        await execute_with_transport(
            make_transport(lambda r: resp), settings, mode=ProviderExecutionMode.WEB_GROUNDED
        )

    ev = exc_info.value.evidence
    assert ev is not None
    assert ev.observed_web_tool_call_count is None
    assert ev.search_action_count is None
    assert ev.unknown_web_action_count is None
    assert ev.search_used is False


async def test_single_built_in_tool_assumption_documented() -> None:
    """The request body configures exactly ONE built-in tool (web_search).
    This is the precondition that lets ``web_tool_call_count`` stand in for
    the total built-in tool-call count that ``max_tool_calls`` bounds.
    If this test fails, the bound enforcement must be generalized."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _web_response(_ws("ws_1", "search"))

    await execute_with_transport(
        make_transport(handler),
        make_settings(openai_web_search_max_tool_calls=2),
        mode=ProviderExecutionMode.WEB_GROUNDED,
    )

    tools = captured["body"]["tools"]
    assert len(tools) == 1
    assert tools[0]["type"] == "web_search"
    assert captured["body"]["max_tool_calls"] == 2
