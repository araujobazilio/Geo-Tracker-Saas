"""Unit tests for the Anthropic Messages API adapter.

All tests use httpx.MockTransport to avoid real network calls and assert
the adapter's request envelope, response parsing, error mapping, and
configuration validation behavior.

Covers the UPDATED adapter behavior:
- provider_request_id = request-id HTTP header (not JSON id).
- provider_response_id = JSON id field (new).
- cache_read_input_tokens -> cached_input_tokens.
- cache_creation_input_tokens -> cache_write_input_tokens.
- stop_reason == "pause_turn" -> ProviderSearchError.
- WEB_GROUNDED without observed search -> ProviderSearchError.
- Malformed JSON -> ProviderResponseError (not JSONDecodeError).
- Default web_search tool version web_search_20260318.
- WEB_GROUNDED forces tool_choice {"type": "tool", "name": "web_search"}.
"""

from __future__ import annotations

import json as _json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.core.enums import LLMProvider, ProviderExecutionMode, ProviderSurface
from app.providers.anthropic_adapter import AnthropicProviderAdapter
from app.providers.base import ProviderRequest
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderSearchError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


def make_settings(**overrides: Any) -> Settings:
    """Build a Settings instance with Anthropic defaults for tests."""
    defaults: dict[str, Any] = {
        "app_env": "test",
        "anthropic_api_key": SecretStr("sk-ant-test-key-12345"),
        "anthropic_scan_model": "claude-sonnet-4-5-20250929",
        "anthropic_base_url": "https://api.anthropic.com",
        "anthropic_web_search_tool_version": "web_search_20260318",
        "anthropic_web_search_max_uses": 5,
        "provider_max_output_tokens": 4096,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def execute_with_transport(
    transport: httpx.MockTransport,
    settings: Settings,
    prompt: str = "test prompt",
    mode: ProviderExecutionMode = ProviderExecutionMode.MODEL_ONLY,
) -> Any:
    """Create an adapter with the given transport and execute a request.

    Returns the coroutine to be awaited by the caller.
    """
    adapter = AnthropicProviderAdapter(settings=settings, transport=transport)
    return adapter.execute(ProviderRequest(prompt=prompt, mode=mode))


def _ok_response(
    *,
    content: list[dict[str, Any]] | None = None,
    response_id: str = "msg_123",
    request_id_header: str = "req_abc123",
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
) -> httpx.Response:
    """Build a nominal 200 Anthropic response with a request-id header."""
    if content is None:
        content = [{"type": "text", "text": "Hello from Claude"}]
    if usage is None:
        usage = {"input_tokens": 10, "output_tokens": 5}
    body = {
        "id": response_id,
        "model": "claude-sonnet-4-5-20250929",
        "content": content,
        "stop_reason": stop_reason,
        "usage": usage,
    }
    return httpx.Response(200, json=body, headers={"request-id": request_id_header})


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_only_success() -> None:
    """MODEL_ONLY returns parsed text, usage, and metadata.

    provider_request_id comes from the request-id header; provider_response_id
    comes from the JSON id field. search_used is False.
    """
    handler_response = {
        "id": "msg_123",
        "model": "claude-sonnet-4-5-20250929",
        "content": [{"type": "text", "text": "Hello from Claude"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=handler_response, headers={"request-id": "req_abc123"})

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    result = await execute_with_transport(transport, settings, prompt="Hello")

    assert result.response_text == "Hello from Claude"
    assert result.provider == LLMProvider.ANTHROPIC
    assert result.surface == ProviderSurface.ANTHROPIC_MESSAGES_API
    assert result.execution_mode == ProviderExecutionMode.MODEL_ONLY
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5
    assert result.search_used is False
    assert result.citations == ()
    assert result.finish_reason == "end_turn"
    assert result.provider_request_id == "req_abc123"
    assert result.provider_response_id == "msg_123"


@pytest.mark.asyncio
async def test_web_grounded_success() -> None:
    """WEB_GROUNDED returns search_used=True, citations, and search requests."""
    handler_response = {
        "id": "msg_web_1",
        "model": "claude-sonnet-4-5-20250929",
        "content": [
            {
                "type": "server_tool_use",
                "name": "web_search",
                "input": {"query": "best seo tools"},
            },
            {
                "type": "web_search_tool_result",
                "content": [
                    {"type": "web_search_result", "title": "Result A", "url": "https://a.com"},
                ],
            },
            {
                "type": "text",
                "text": "Based on web search results.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": "https://a.com",
                        "title": "Result A",
                        "cited_text": "Result A is the best.",
                    }
                ],
            },
        ],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 20,
            "output_tokens": 15,
            "server_tool_use": {"web_search_requests": 2},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=handler_response, headers={"request-id": "req_web_1"})

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    result = await execute_with_transport(
        transport, settings, prompt="search query", mode=ProviderExecutionMode.WEB_GROUNDED
    )

    assert result.search_used is True
    assert result.execution_mode == ProviderExecutionMode.WEB_GROUNDED
    assert len(result.citations) == 1
    assert result.citations[0].url == "https://a.com"
    assert result.citations[0].title == "Result A"
    assert result.citations[0].cited_text == "Result A is the best."
    assert result.usage.search_requests == 2
    assert result.response_text == "Based on web search results."
    assert result.provider_request_id == "req_web_1"
    assert result.provider_response_id == "msg_web_1"


@pytest.mark.asyncio
async def test_web_grounded_forces_tool_choice() -> None:
    """WEB_GROUNDED sends tool_choice to force the web_search tool."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return _ok_response(
            content=[
                {"type": "server_tool_use", "name": "web_search", "input": {"query": "q"}},
                {"type": "web_search_tool_result", "content": []},
                {"type": "text", "text": "ok"},
            ],
            response_id="msg_force",
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    await execute_with_transport(transport, settings, mode=ProviderExecutionMode.WEB_GROUNDED)

    assert captured["body"]["tool_choice"] == {"type": "tool", "name": "web_search"}


@pytest.mark.asyncio
async def test_web_grounded_without_search_raises_error() -> None:
    """WEB_GROUNDED with a normal text response (no search) raises ProviderSearchError."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return _ok_response(
            content=[{"type": "text", "text": "No search happened here."}],
            response_id="msg_nosearch",
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    with pytest.raises(ProviderSearchError):
        await execute_with_transport(transport, settings, mode=ProviderExecutionMode.WEB_GROUNDED)

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_pause_turn_raises_error() -> None:
    """A 200 response with stop_reason == 'pause_turn' raises ProviderSearchError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(
            content=[{"type": "text", "text": "partial"}],
            stop_reason="pause_turn",
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    with pytest.raises(ProviderSearchError):
        await execute_with_transport(transport, settings)


@pytest.mark.asyncio
async def test_request_id_header_parsed() -> None:
    """provider_request_id comes from the request-id HTTP response header."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_xyz",
                "model": "claude-sonnet-4-5-20250929",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            headers={"request-id": "req_header_999"},
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    result = await execute_with_transport(transport, settings)

    assert result.provider_request_id == "req_header_999"


@pytest.mark.asyncio
async def test_message_id_parsed() -> None:
    """provider_response_id comes from the JSON id field."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_unique_456",
                "model": "claude-sonnet-4-5-20250929",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            headers={"request-id": "req_abc123"},
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    result = await execute_with_transport(transport, settings)

    assert result.provider_response_id == "msg_unique_456"


@pytest.mark.asyncio
async def test_cache_read_tokens() -> None:
    """usage.cache_read_input_tokens maps to cached_input_tokens."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(
            usage={"input_tokens": 100, "output_tokens": 5, "cache_read_input_tokens": 100},
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    result = await execute_with_transport(transport, settings)

    assert result.usage.cached_input_tokens == 100


@pytest.mark.asyncio
async def test_cache_creation_tokens() -> None:
    """usage.cache_creation_input_tokens maps to cache_write_input_tokens."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(
            usage={"input_tokens": 100, "output_tokens": 5, "cache_creation_input_tokens": 50},
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    result = await execute_with_transport(transport, settings)

    assert result.usage.cache_write_input_tokens == 50


@pytest.mark.asyncio
async def test_exact_prompt_preserved() -> None:
    """The prompt is sent verbatim in messages[0].content."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return _ok_response(response_id="msg_p")

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    exact_prompt = "Tell me about GEO tracking  \nwith special chars: \u00e9\u00e8\u00e0"
    await execute_with_transport(transport, settings, prompt=exact_prompt)

    assert captured["body"]["messages"][0]["content"] == exact_prompt


@pytest.mark.asyncio
async def test_citations_parsed() -> None:
    """Two distinct citations are parsed into ProviderCitation objects."""
    handler_response = {
        "id": "msg_cite",
        "model": "claude-sonnet-4-5-20250929",
        "content": [
            {
                "type": "text",
                "text": "See sources.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": "https://example.com/a",
                        "title": "Source A",
                        "cited_text": "Content A.",
                    },
                    {
                        "type": "web_search_result_location",
                        "url": "https://example.com/b",
                        "title": "Source B",
                        "cited_text": "Content B.",
                    },
                ],
            }
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(
            content=handler_response["content"],
            response_id="msg_cite",
            usage={"input_tokens": 5, "output_tokens": 3},
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    result = await execute_with_transport(transport, settings)

    assert len(result.citations) == 2
    assert result.citations[0].url == "https://example.com/a"
    assert result.citations[0].title == "Source A"
    assert result.citations[0].cited_text == "Content A."
    assert result.citations[1].url == "https://example.com/b"
    assert result.citations[1].title == "Source B"
    assert result.citations[1].cited_text == "Content B."


@pytest.mark.asyncio
async def test_citation_deduplication() -> None:
    """Citations with the same URL are deduplicated, preserving order."""
    handler_response = {
        "id": "msg_dedup",
        "model": "claude-sonnet-4-5-20250929",
        "content": [
            {
                "type": "text",
                "text": "First mention.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": "https://dup.com",
                        "title": "Dup",
                        "cited_text": "First.",
                    }
                ],
            },
            {
                "type": "text",
                "text": "Second mention.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "url": "https://dup.com",
                        "title": "Dup Again",
                        "cited_text": "Second.",
                    }
                ],
            },
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(
            content=handler_response["content"],
            response_id="msg_dedup",
            usage={"input_tokens": 5, "output_tokens": 3},
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    result = await execute_with_transport(transport, settings)

    assert len(result.citations) == 1
    assert result.citations[0].url == "https://dup.com"
    assert result.citations[0].title == "Dup"


@pytest.mark.asyncio
async def test_usage_with_search_requests() -> None:
    """server_tool_use.web_search_requests maps to usage.search_requests."""
    handler_response = {
        "id": "msg_usage",
        "model": "claude-sonnet-4-5-20250929",
        "content": [
            {
                "type": "server_tool_use",
                "name": "web_search",
                "input": {"query": "test"},
            },
            {
                "type": "web_search_tool_result",
                "content": [
                    {"type": "web_search_result", "title": "R", "url": "https://r.com"},
                ],
            },
            {"type": "text", "text": "Result."},
        ],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 12,
            "output_tokens": 8,
            "server_tool_use": {"web_search_requests": 3},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(
            content=handler_response["content"],
            response_id="msg_usage",
            usage=handler_response["usage"],
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    result = await execute_with_transport(
        transport, settings, mode=ProviderExecutionMode.WEB_GROUNDED
    )

    assert result.usage.search_requests == 3
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 8


# ---------------------------------------------------------------------------
# Body-level search error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_body_level_search_error() -> None:
    """A web_search_tool_result_error block raises ProviderSearchError."""
    handler_response = {
        "id": "msg_err",
        "model": "claude-sonnet-4-5-20250929",
        "content": [
            {
                "type": "web_search_tool_result",
                "content": {
                    "type": "web_search_tool_result_error",
                    "error_code": "max_uses_exceeded",
                },
            }
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(
            content=handler_response["content"],
            response_id="msg_err",
            usage={"input_tokens": 5, "output_tokens": 1},
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    with pytest.raises(ProviderSearchError):
        await execute_with_transport(transport, settings, mode=ProviderExecutionMode.WEB_GROUNDED)


# ---------------------------------------------------------------------------
# HTTP error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401() -> None:
    """401 response raises ProviderAuthenticationError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    with pytest.raises(ProviderAuthenticationError):
        await execute_with_transport(transport, settings)


@pytest.mark.asyncio
async def test_429() -> None:
    """429 response with Retry-After raises ProviderRateLimitError with retry_after_seconds."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "30"},
            json={"error": "rate_limited"},
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    with pytest.raises(ProviderRateLimitError) as exc_info:
        await execute_with_transport(transport, settings)

    assert exc_info.value.retry_after_seconds == 30.0


@pytest.mark.asyncio
async def test_500() -> None:
    """500 response raises ProviderUnavailableError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server_error"})

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    with pytest.raises(ProviderUnavailableError):
        await execute_with_transport(transport, settings)


# ---------------------------------------------------------------------------
# Transport errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout() -> None:
    """A transport TimeoutException raises ProviderTimeoutError."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("read timed out")

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    with pytest.raises(ProviderTimeoutError):
        await execute_with_transport(transport, settings)


# ---------------------------------------------------------------------------
# Malformed response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_body() -> None:
    """A 200 response with no content blocks raises ProviderResponseError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "msg", "model": "claude-sonnet-4-5-20250929", "stop_reason": "end_turn"},
            headers={"request-id": "req_m"},
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    with pytest.raises(ProviderResponseError):
        await execute_with_transport(transport, settings)


@pytest.mark.asyncio
async def test_invalid_json_response() -> None:
    """A 200 response with genuinely invalid JSON raises ProviderResponseError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"not json{{{",
            headers={"content-type": "application/json", "request-id": "req_bad"},
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    with pytest.raises(ProviderResponseError):
        await execute_with_transport(transport, settings)


# ---------------------------------------------------------------------------
# No automatic retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_automatic_retry() -> None:
    """The adapter performs exactly one HTTP request (no retries)."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(500, json={"error": "server_error"})

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    with pytest.raises(ProviderUnavailableError):
        await execute_with_transport(transport, settings)

    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_api_key() -> None:
    """An empty API key raises ProviderConfigurationError before any call."""
    settings = make_settings(anthropic_api_key=SecretStr(""))

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(content=[])

    transport = httpx.MockTransport(handler)
    with pytest.raises(ProviderConfigurationError):
        await execute_with_transport(transport, settings)


@pytest.mark.asyncio
async def test_missing_model() -> None:
    """An empty scan model raises ProviderConfigurationError before any call."""
    settings = make_settings(anthropic_scan_model="")

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(content=[])

    transport = httpx.MockTransport(handler)
    with pytest.raises(ProviderConfigurationError):
        await execute_with_transport(transport, settings)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def test_capabilities() -> None:
    """capabilities() returns the expected Anthropic capability set."""
    adapter = AnthropicProviderAdapter(settings=make_settings())
    caps = adapter.capabilities()

    assert caps.supports_model_only is True
    assert caps.supports_web_grounded is True
    assert caps.supports_citations is True
    assert caps.supports_search_result_metadata is True


# ---------------------------------------------------------------------------
# repr safety
# ---------------------------------------------------------------------------


def test_repr_no_api_key() -> None:
    """repr must not leak the API key."""
    adapter = AnthropicProviderAdapter(settings=make_settings())
    assert "sk-ant-test-key-12345" not in repr(adapter)


# ---------------------------------------------------------------------------
# WEB_GROUNDED tool configuration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_grounded_tool_config() -> None:
    """WEB_GROUNDED request body includes the web_search tool config."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return _ok_response(
            content=[
                {"type": "server_tool_use", "name": "web_search", "input": {"query": "q"}},
                {"type": "web_search_tool_result", "content": []},
                {"type": "text", "text": "ok"},
            ],
            response_id="msg_tool",
        )

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    await execute_with_transport(transport, settings, mode=ProviderExecutionMode.WEB_GROUNDED)

    body = captured["body"]
    assert "tools" in body
    tools = body["tools"]
    assert isinstance(tools, list)
    assert len(tools) == 1
    assert tools[0] == {
        "type": "web_search_20260318",
        "name": "web_search",
        "max_uses": 5,
        "allowed_callers": ["direct"],
    }
    assert body["tool_choice"] == {"type": "tool", "name": "web_search"}


@pytest.mark.asyncio
async def test_alternative_web_search_version_uses_direct_call() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return _ok_response(
            content=[
                {"type": "server_tool_use", "name": "web_search", "input": {"query": "q"}},
                {"type": "web_search_tool_result", "content": []},
                {"type": "text", "text": "ok"},
            ]
        )

    settings = make_settings(anthropic_web_search_tool_version="web_search_20250305")
    await execute_with_transport(
        httpx.MockTransport(handler),
        settings,
        mode=ProviderExecutionMode.WEB_GROUNDED,
    )

    assert captured["body"]["tools"] == [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
            "allowed_callers": ["direct"],
        }
    ]
    assert captured["body"]["tool_choice"] == {"type": "tool", "name": "web_search"}


# ---------------------------------------------------------------------------
# max_tokens sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_tokens_sent() -> None:
    """The request body includes the configured max_tokens value."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content.decode("utf-8"))
        return _ok_response(response_id="msg_mt")

    transport = httpx.MockTransport(handler)
    settings = make_settings()
    await execute_with_transport(transport, settings)

    assert captured["body"]["max_tokens"] == 4096
