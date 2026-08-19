"""Comprehensive MockTransport tests for the Perplexity Sonar API adapter.

These tests inject an ``httpx.MockTransport`` into ``PerplexityProviderAdapter``
so that no real network call is ever made. They cover the UPDATED adapter
contract:

- WEB_GROUNDED success path (Sonar is web-grounded only)
- Request envelope construction (model, prompt preserved exactly, max_tokens)
- Citation parsing from search_results (primary) and legacy citations (fallback)
- Citation deduplication by URL
- Usage parsing including citation_tokens, reasoning_tokens, search_requests
- Provider-reported cost (Decimal) from usage.cost.total_cost
- provider_request_id from X-Request-ID HTTP response header
- provider_response_id from response JSON `id` field
- MODEL_ONLY rejected BEFORE any network call
- HTTP error mapping (401, 429, 500)
- Transport error mapping (timeout)
- Malformed body and invalid JSON → ProviderResponseError
- Configuration validation (missing key/model)
- No automatic retry semantics
- Capability and repr safety checks
"""

from __future__ import annotations

import json
from decimal import Decimal
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
    defaults: dict[str, Any] = {
        "app_env": "test",
        "perplexity_api_key": SecretStr("pplx-test-key-12345"),
        "perplexity_scan_model": "sonar-pro",
        "perplexity_base_url": "https://api.perplexity.ai",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def execute_with_transport(
    transport: httpx.AsyncBaseTransport,
    settings: Settings,
    prompt: str = "test prompt",
    mode: ProviderExecutionMode = ProviderExecutionMode.WEB_GROUNDED,
    model: str | None = None,
    max_output_tokens: int | None = None,
) -> ProviderResult:
    adapter = PerplexityProviderAdapter(settings=settings, transport=transport)
    request = ProviderRequest(
        prompt=prompt,
        mode=mode,
        model=model,
        max_output_tokens=max_output_tokens,
    )
    return await adapter.execute(request)


def _full_usage() -> dict[str, Any]:
    return {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "num_search_queries": 2,
        "citation_tokens": 5,
        "reasoning_tokens": 8,
        "cost": {
            "input_tokens_cost": 0.0,
            "output_tokens_cost": 0.012,
            "request_cost": 0.006,
            "total_cost": 0.019,
        },
    }


def _success_body(
    *,
    content: str = "Search-grounded answer",
    model: str = "sonar-pro",
    completion_id: str = "comp_123",
    search_results: list | None = None,
    citations: list | None = None,
    usage: dict | None = None,
    choices: list | None = None,
    include_search_results: bool = True,
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
        usage = _full_usage()
    if choices is None:
        choices = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ]
    body: dict[str, Any] = {
        "id": completion_id,
        "model": model,
        "choices": choices,
        "usage": usage,
    }
    if include_search_results:
        body["search_results"] = search_results
    if citations is not None:
        body["citations"] = citations
    return body


def _success_response(
    body: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    if body is None:
        body = _success_body()
    if headers is None:
        headers = {"X-Request-ID": "pplx_req_abc"}
    else:
        headers = {**{"X-Request-ID": "pplx_req_abc"}, **headers}
    return httpx.Response(200, json=body, headers=headers)


# ---------------------------------------------------------------------------
# 1. Full success path
# ---------------------------------------------------------------------------


async def test_web_grounded_success():
    body = _success_body()
    transport = make_transport(lambda req: _success_response(body))
    result = await execute_with_transport(transport, make_settings())

    assert result.response_text == "Search-grounded answer"
    assert result.provider == LLMProvider.PERPLEXITY
    assert result.surface == ProviderSurface.PERPLEXITY_SONAR_API
    assert result.execution_mode == ProviderExecutionMode.WEB_GROUNDED
    assert result.requested_model == "sonar-pro"
    assert result.returned_model == "sonar-pro"
    assert result.finish_reason == "stop"
    assert result.search_used is True
    assert result.metadata == {}
    assert result.latency_ms >= 0

    # IDs: request ID from header, response ID from JSON id.
    assert result.provider_request_id == "pplx_req_abc"
    assert result.provider_response_id == "comp_123"

    # Usage fields.
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 20
    assert result.usage.total_tokens == 30
    assert result.usage.search_requests == 2
    assert result.usage.citation_tokens == 5
    assert result.usage.reasoning_tokens == 8

    # Provider-reported cost (Decimal).
    assert result.provider_reported_cost_usd == Decimal("0.019")

    # Citations from search_results.
    assert len(result.citations) == 1
    assert result.citations[0].url == "https://example.com/1"
    assert result.citations[0].title == "Source 1"
    assert result.citations[0].source_type == "web"


# ---------------------------------------------------------------------------
# 2. Exact prompt preserved
# ---------------------------------------------------------------------------


async def test_exact_prompt_preserved():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return _success_response()

    transport = make_transport(handler)
    prompt = "What is the latest SERP feature for 'best coffee shops in Lisbon'?"
    await execute_with_transport(transport, make_settings(), prompt=prompt)

    messages = captured["body"]["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == prompt


# ---------------------------------------------------------------------------
# 3. Configured model used
# ---------------------------------------------------------------------------


async def test_configured_model_used():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return _success_response(_success_body(model="my-sonar-model"))

    transport = make_transport(handler)
    settings = make_settings(perplexity_scan_model="my-sonar-model")
    result = await execute_with_transport(transport, settings)

    assert captured["body"]["model"] == "my-sonar-model"
    assert result.requested_model == "my-sonar-model"


# ---------------------------------------------------------------------------
# 4. max_tokens sent in request body
# ---------------------------------------------------------------------------


async def test_max_tokens_sent():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return _success_response()

    transport = make_transport(handler)
    # Custom request value overrides settings default.
    await execute_with_transport(transport, make_settings(), max_output_tokens=1234)

    assert captured["body"]["max_tokens"] == 1234


async def test_max_tokens_settings_default():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return _success_response()

    transport = make_transport(handler)
    settings = make_settings(provider_max_output_tokens=2048)
    # No explicit request value → settings default used.
    await execute_with_transport(transport, settings)

    assert captured["body"]["max_tokens"] == 2048


# ---------------------------------------------------------------------------
# 5. Search results parsing (3 → 3 citations)
# ---------------------------------------------------------------------------


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
    transport = make_transport(lambda req: _success_response(body))
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


# ---------------------------------------------------------------------------
# 6. Citation deduplication by URL
# ---------------------------------------------------------------------------


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
    transport = make_transport(lambda req: _success_response(body))
    result = await execute_with_transport(transport, make_settings())

    assert len(result.citations) == 1
    assert result.citations[0].url == "https://example.com/dup"
    assert result.citations[0].title == "Source 1"


# ---------------------------------------------------------------------------
# 7. search_results present, no citations field → citations from search_results
# ---------------------------------------------------------------------------


async def test_search_results_without_citations_field():
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
    transport = make_transport(lambda req: _success_response(body))
    result = await execute_with_transport(transport, make_settings())

    assert "citations" not in body
    assert len(result.citations) == 2
    assert result.citations[0].url == "https://example.com/x"
    assert result.citations[0].title == "Source X"
    assert result.citations[0].source_type == "web"
    assert result.citations[1].url == "https://example.com/y"
    assert result.citations[1].title == "Source Y"
    assert result.citations[1].source_type == "news"


# ---------------------------------------------------------------------------
# 8. Legacy citations fallback (no search_results)
# ---------------------------------------------------------------------------


async def test_legacy_citations_fallback():
    body = _success_body(include_search_results=False, citations=["https://a.com", "https://b.com"])
    transport = make_transport(lambda req: _success_response(body))
    result = await execute_with_transport(transport, make_settings())

    assert len(result.citations) == 2
    assert result.citations[0].url == "https://a.com"
    assert result.citations[0].title is None
    assert result.citations[1].url == "https://b.com"
    assert result.citations[1].title is None


# ---------------------------------------------------------------------------
# 9. Usage parsing (all fields)
# ---------------------------------------------------------------------------


async def test_usage_parsing():
    usage = {
        "prompt_tokens": 42,
        "completion_tokens": 58,
        "total_tokens": 100,
        "num_search_queries": 5,
        "citation_tokens": 7,
        "reasoning_tokens": 11,
    }
    body = _success_body(usage=usage)
    transport = make_transport(lambda req: _success_response(body))
    result = await execute_with_transport(transport, make_settings())

    assert result.usage.input_tokens == 42
    assert result.usage.output_tokens == 58
    assert result.usage.total_tokens == 100
    assert result.usage.search_requests == 5
    assert result.usage.citation_tokens == 7
    assert result.usage.reasoning_tokens == 11


# ---------------------------------------------------------------------------
# 10. citation_tokens parsed
# ---------------------------------------------------------------------------


async def test_citation_tokens_parsed():
    usage = _full_usage()
    usage["citation_tokens"] = 5
    body = _success_body(usage=usage)
    transport = make_transport(lambda req: _success_response(body))
    result = await execute_with_transport(transport, make_settings())

    assert result.usage.citation_tokens == 5


# ---------------------------------------------------------------------------
# 11. reasoning_tokens parsed
# ---------------------------------------------------------------------------


async def test_reasoning_tokens_parsed():
    usage = _full_usage()
    usage["reasoning_tokens"] = 8
    body = _success_body(usage=usage)
    transport = make_transport(lambda req: _success_response(body))
    result = await execute_with_transport(transport, make_settings())

    assert result.usage.reasoning_tokens == 8


# ---------------------------------------------------------------------------
# 12. provider_reported_cost from usage.cost.total_cost
# ---------------------------------------------------------------------------


async def test_provider_reported_cost():
    body = _success_body()
    transport = make_transport(lambda req: _success_response(body))
    result = await execute_with_transport(transport, make_settings())

    assert result.provider_reported_cost_usd == Decimal("0.019")


# ---------------------------------------------------------------------------
# 13. provider_reported_cost None when cost absent
# ---------------------------------------------------------------------------


async def test_provider_reported_cost_none_when_absent():
    usage = _full_usage()
    usage.pop("cost", None)
    body = _success_body(usage=usage)
    transport = make_transport(lambda req: _success_response(body))
    result = await execute_with_transport(transport, make_settings())

    assert result.provider_reported_cost_usd is None


# ---------------------------------------------------------------------------
# 14. X-Request-ID header parsed as provider_request_id
# ---------------------------------------------------------------------------


async def test_x_request_id_parsed():
    body = _success_body()
    transport = make_transport(
        lambda req: httpx.Response(200, json=body, headers={"X-Request-ID": "pplx_req_abc"})
    )
    result = await execute_with_transport(transport, make_settings())

    assert result.provider_request_id == "pplx_req_abc"


# ---------------------------------------------------------------------------
# 15. JSON id parsed as provider_response_id
# ---------------------------------------------------------------------------


async def test_completion_id_parsed():
    body = _success_body(completion_id="comp_xyz_999")
    transport = make_transport(lambda req: _success_response(body))
    result = await execute_with_transport(transport, make_settings())

    assert result.provider_response_id == "comp_xyz_999"


# ---------------------------------------------------------------------------
# 16. MODEL_ONLY rejected before network call
# ---------------------------------------------------------------------------


async def test_model_only_rejected_before_network():
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return _success_response()

    transport = make_transport(handler)
    adapter = PerplexityProviderAdapter(settings=make_settings(), transport=transport)
    request = ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)
    with pytest.raises(ProviderModeNotAllowedError):
        await adapter.execute(request)
    assert call_count[0] == 0, "Perplexity MODEL_ONLY must not make any network call"


# ---------------------------------------------------------------------------
# 17. Missing API key
# ---------------------------------------------------------------------------


async def test_missing_api_key():
    settings = make_settings(perplexity_api_key=SecretStr(""))
    transport = make_transport(lambda req: _success_response())
    with pytest.raises(ProviderConfigurationError):
        await execute_with_transport(transport, settings)


# ---------------------------------------------------------------------------
# 18. Missing model
# ---------------------------------------------------------------------------


async def test_missing_model():
    settings = make_settings(perplexity_scan_model="")
    transport = make_transport(lambda req: _success_response())
    with pytest.raises(ProviderConfigurationError):
        await execute_with_transport(transport, settings)


# ---------------------------------------------------------------------------
# 19. 401 → ProviderAuthenticationError
# ---------------------------------------------------------------------------


async def test_401():
    transport = make_transport(lambda req: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(ProviderAuthenticationError):
        await execute_with_transport(transport, make_settings())


# ---------------------------------------------------------------------------
# 20. 429 → ProviderRateLimitError with retry_after_seconds
# ---------------------------------------------------------------------------


async def test_429():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "15"}, json={"error": "rate"})

    transport = make_transport(handler)
    with pytest.raises(ProviderRateLimitError) as exc_info:
        await execute_with_transport(transport, make_settings())

    assert exc_info.value.retry_after_seconds == 15.0


# ---------------------------------------------------------------------------
# 21. 500 → ProviderUnavailableError
# ---------------------------------------------------------------------------


async def test_500():
    transport = make_transport(lambda req: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(ProviderUnavailableError):
        await execute_with_transport(transport, make_settings())


# ---------------------------------------------------------------------------
# 22. Timeout → ProviderTimeoutError
# ---------------------------------------------------------------------------


async def test_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout")

    transport = make_transport(handler)
    with pytest.raises(ProviderTimeoutError):
        await execute_with_transport(transport, make_settings())


# ---------------------------------------------------------------------------
# 23. Malformed body (empty choices) → ProviderResponseError
# ---------------------------------------------------------------------------


async def test_malformed_body():
    body = {"id": "x", "model": "sonar", "choices": []}
    transport = make_transport(lambda req: _success_response(body))
    with pytest.raises(ProviderResponseError):
        await execute_with_transport(transport, make_settings())


# ---------------------------------------------------------------------------
# 24. Invalid JSON response → ProviderResponseError
# ---------------------------------------------------------------------------


async def test_invalid_json_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"not json{{{",
            headers={"content-type": "application/json"},
        )

    transport = make_transport(handler)
    with pytest.raises(ProviderResponseError):
        await execute_with_transport(transport, make_settings())


# ---------------------------------------------------------------------------
# 25. No automatic retry (single call on 500)
# ---------------------------------------------------------------------------


async def test_no_automatic_retry():
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(500, json={"error": "boom"})

    transport = make_transport(handler)
    with pytest.raises(ProviderUnavailableError):
        await execute_with_transport(transport, make_settings())

    assert call_count[0] == 1, "Perplexity adapter must not retry automatically"


# ---------------------------------------------------------------------------
# 26. Capabilities
# ---------------------------------------------------------------------------


async def test_capabilities():
    adapter = PerplexityProviderAdapter(settings=make_settings())
    caps = adapter.capabilities()

    assert caps.supports_model_only is False
    assert caps.supports_web_grounded is True


# ---------------------------------------------------------------------------
# 27. Repr does not leak API key
# ---------------------------------------------------------------------------


async def test_repr_no_api_key():
    adapter = PerplexityProviderAdapter(settings=make_settings())
    repr_str = repr(adapter)

    assert "pplx-test-key-12345" not in repr_str
