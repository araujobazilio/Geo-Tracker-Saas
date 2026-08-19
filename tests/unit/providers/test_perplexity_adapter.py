"""Comprehensive MockTransport tests for the Perplexity Sonar API adapter.

These tests inject an ``httpx.MockTransport`` into ``PerplexityProviderAdapter``
so that no real network call is ever made. They cover:
- WEB_GROUNDED success path (Sonar is web-grounded only)
- Request envelope construction (model, prompt preserved exactly)
- Citation parsing from search_results (primary) and legacy citations (fallback)
- Citation deduplication by URL
- Usage parsing (prompt/completion/total tokens, num_search_queries)
- MODEL_ONLY rejected BEFORE any network call
- HTTP error mapping (401, 429, 500)
- Transport error mapping (timeout)
- Configuration validation (missing key/model)
- No automatic retry semantics
- Capability and repr safety checks
"""

from __future__ import annotations

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
    ProviderModeNotAllowedError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.perplexity_adapter import PerplexityProviderAdapter

pytestmark = pytest.mark.asyncio


def make_settings(**overrides: Any) -> Settings:
    defaults = {
        "app_env": "test",
        "perplexity_api_key": SecretStr("pplx-test-key-12345"),
        "perplexity_scan_model": "sonar-pro",
        "perplexity_base_url": "https://api.perplexity.ai",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def execute_with_transport(
    transport: httpx.AsyncBaseTransport,
    settings: Settings,
    prompt: str = "test prompt",
    mode: ProviderExecutionMode = ProviderExecutionMode.WEB_GROUNDED,
    model: str | None = None,
) -> ProviderResult:
    adapter = PerplexityProviderAdapter(settings=settings, transport=transport)
    request = ProviderRequest(prompt=prompt, mode=mode, model=model)
    return await adapter.execute(request)


def _success_body(
    *,
    content: str = "Search-grounded answer",
    model: str = "sonar-pro",
    request_id: str = "comp_123",
    search_results: list | None = None,
    citations: list | None = None,
    usage: dict | None = None,
    choices: list | None = None,
) -> dict:
    if search_results is None:
        search_results = [
            {
                "title": "Source 1",
                "url": "https://example.com/1",
                "date": "2025-01-01",
                "snippet": "Snippet 1",
                "source": "web",
            }
        ]
    if usage is None:
        usage = {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "num_search_queries": 2,
        }
    if choices is None:
        choices = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ]
    body: dict = {
        "id": request_id,
        "model": model,
        "choices": choices,
        "usage": usage,
        "search_results": search_results,
    }
    if citations is not None:
        body["citations"] = citations
    return body


async def test_web_grounded_success():
    body = _success_body()
    transport = make_transport(lambda req: httpx.Response(200, json=body))
    result = await execute_with_transport(transport, make_settings())

    assert result.response_text == "Search-grounded answer"
    assert result.provider == LLMProvider.PERPLEXITY
    assert result.surface == ProviderSurface.PERPLEXITY_SONAR_API
    assert result.execution_mode == ProviderExecutionMode.WEB_GROUNDED
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 20
    assert result.usage.total_tokens == 30
    assert result.usage.search_requests == 2
    assert result.search_used is True
    assert result.returned_model == "sonar-pro"
    assert result.provider_request_id == "comp_123"
    assert result.finish_reason == "stop"
    assert len(result.citations) == 1
    assert result.citations[0].url == "https://example.com/1"
    assert result.citations[0].title == "Source 1"
    assert result.citations[0].source_type == "web"


async def test_exact_prompt_preserved():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_success_body())

    transport = make_transport(handler)
    prompt = "What is the latest SERP feature for 'best coffee shops in Lisbon'?"
    await execute_with_transport(transport, make_settings(), prompt=prompt)

    messages = captured["body"]["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == prompt


async def test_configured_model_used():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json=_success_body(model="my-sonar-model"))

    transport = make_transport(handler)
    settings = make_settings(perplexity_scan_model="my-sonar-model")
    result = await execute_with_transport(transport, settings)

    assert captured["body"]["model"] == "my-sonar-model"
    assert result.requested_model == "my-sonar-model"


async def test_search_results_parsing():
    search_results = [
        {
            "title": "Source A",
            "url": "https://example.com/a",
            "date": "2025-01-01",
            "snippet": "Snippet A",
            "source": "web",
        },
        {
            "title": "Source B",
            "url": "https://example.com/b",
            "date": "2025-01-02",
            "snippet": "Snippet B",
            "source": "news",
        },
        {
            "title": "Source C",
            "url": "https://example.com/c",
            "date": "2025-01-03",
            "snippet": "Snippet C",
            "source": "academic",
        },
    ]
    body = _success_body(search_results=search_results, citations=[])
    transport = make_transport(lambda req: httpx.Response(200, json=body))
    result = await execute_with_transport(transport, make_settings())

    assert len(result.citations) == 3
    assert result.citations[0].url == "https://example.com/a"
    assert result.citations[0].title == "Source A"
    assert result.citations[0].source_type == "web"
    assert result.citations[1].url == "https://example.com/b"
    assert result.citations[1].title == "Source B"
    assert result.citations[1].source_type == "news"
    assert result.citations[2].url == "https://example.com/c"
    assert result.citations[2].title == "Source C"
    assert result.citations[2].source_type == "academic"


async def test_citation_deduplication():
    search_results = [
        {
            "title": "Source 1",
            "url": "https://example.com/dup",
            "date": "2025-01-01",
            "snippet": "Snippet 1",
            "source": "web",
        },
        {
            "title": "Source 2 (same URL)",
            "url": "https://example.com/dup",
            "date": "2025-01-02",
            "snippet": "Snippet 2",
            "source": "web",
        },
    ]
    body = _success_body(search_results=search_results, citations=[])
    transport = make_transport(lambda req: httpx.Response(200, json=body))
    result = await execute_with_transport(transport, make_settings())

    assert len(result.citations) == 1
    assert result.citations[0].url == "https://example.com/dup"
    assert result.citations[0].title == "Source 1"


async def test_search_results_without_citations_field():
    """Critical test: search_results present, citations field absent.

    The adapter must parse citations from search_results alone.
    """
    search_results = [
        {
            "title": "Source X",
            "url": "https://example.com/x",
            "date": "2025-01-01",
            "snippet": "Snippet X",
            "source": "web",
        },
        {
            "title": "Source Y",
            "url": "https://example.com/y",
            "date": "2025-01-02",
            "snippet": "Snippet Y",
            "source": "news",
        },
    ]
    body = _success_body(search_results=search_results, citations=None)
    # Ensure no citations key is present at all.
    body.pop("citations", None)
    transport = make_transport(lambda req: httpx.Response(200, json=body))
    result = await execute_with_transport(transport, make_settings())

    assert "citations" not in body
    assert len(result.citations) == 2
    assert result.citations[0].url == "https://example.com/x"
    assert result.citations[0].title == "Source X"
    assert result.citations[0].source_type == "web"
    assert result.citations[1].url == "https://example.com/y"
    assert result.citations[1].title == "Source Y"
    assert result.citations[1].source_type == "news"


async def test_legacy_citations_fallback():
    """No search_results; fall back to legacy citations URL array."""
    body = _success_body(search_results=[], citations=["https://a.com", "https://b.com"])
    # Remove search_results entirely to exercise the fallback path.
    body.pop("search_results", None)
    transport = make_transport(lambda req: httpx.Response(200, json=body))
    result = await execute_with_transport(transport, make_settings())

    assert len(result.citations) == 2
    assert result.citations[0].url == "https://a.com"
    assert result.citations[0].title is None
    assert result.citations[1].url == "https://b.com"
    assert result.citations[1].title is None


async def test_usage_parsing():
    usage = {
        "prompt_tokens": 42,
        "completion_tokens": 58,
        "total_tokens": 100,
        "num_search_queries": 5,
    }
    body = _success_body(usage=usage)
    transport = make_transport(lambda req: httpx.Response(200, json=body))
    result = await execute_with_transport(transport, make_settings())

    assert result.usage.input_tokens == 42
    assert result.usage.output_tokens == 58
    assert result.usage.total_tokens == 100
    assert result.usage.search_requests == 5


async def test_model_only_rejected_before_network():
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(200, json={})

    transport = make_transport(handler)
    adapter = PerplexityProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)
    with pytest.raises(ProviderModeNotAllowedError):
        await adapter.execute(request)
    assert call_count[0] == 0, "Perplexity MODEL_ONLY must not make any network call"


async def test_missing_api_key():
    settings = make_settings(perplexity_api_key=SecretStr(""))
    transport = make_transport(lambda req: httpx.Response(200, json=_success_body()))
    with pytest.raises(ProviderConfigurationError):
        await execute_with_transport(transport, settings)


async def test_missing_model():
    settings = make_settings(perplexity_scan_model="")
    transport = make_transport(lambda req: httpx.Response(200, json=_success_body()))
    with pytest.raises(ProviderConfigurationError):
        await execute_with_transport(transport, settings)


async def test_401():
    transport = make_transport(lambda req: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(ProviderAuthenticationError):
        await execute_with_transport(transport, make_settings())


async def test_429():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "15"}, json={"error": "rate"})

    transport = make_transport(handler)
    with pytest.raises(ProviderRateLimitError) as exc_info:
        await execute_with_transport(transport, make_settings())

    assert exc_info.value.retry_after_seconds == 15.0


async def test_500():
    transport = make_transport(lambda req: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(ProviderUnavailableError):
        await execute_with_transport(transport, make_settings())


async def test_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout")

    transport = make_transport(handler)
    with pytest.raises(ProviderTimeoutError):
        await execute_with_transport(transport, make_settings())


async def test_malformed_body():
    body = {"id": "x", "model": "sonar", "choices": []}
    transport = make_transport(lambda req: httpx.Response(200, json=body))
    with pytest.raises(ProviderResponseError):
        await execute_with_transport(transport, make_settings())


async def test_no_automatic_retry():
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(500, json={"error": "boom"})

    transport = make_transport(handler)
    with pytest.raises(ProviderUnavailableError):
        await execute_with_transport(transport, make_settings())

    assert call_count[0] == 1, "Perplexity adapter must not retry automatically"


async def test_capabilities():
    adapter = PerplexityProviderAdapter(settings=make_settings())
    caps = adapter.capabilities()

    assert caps.supports_model_only is False
    assert caps.supports_web_grounded is True
    assert caps.supports_citations is True
    assert caps.supports_search_result_metadata is True


async def test_repr_no_api_key():
    adapter = PerplexityProviderAdapter(settings=make_settings())
    repr_str = repr(adapter)

    assert "pplx-test-key-12345" not in repr_str
