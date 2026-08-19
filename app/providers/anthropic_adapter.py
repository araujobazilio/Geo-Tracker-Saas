"""Anthropic Messages API adapter.

Implements the ProviderAdapter protocol against the Anthropic Messages API
(POST /v1/messages). This adapter is a thin infrastructure service:

- Exactly ONE HTTP request per execute() call (no automatic retries).
- No quota/usage accounting, no pricing, no persistence.
- No system prompt distortion: only the minimum API envelope is sent.
- Does not parse chain-of-thought/reasoning blocks.
- API key never appears in repr, logs, or exceptions.
- WEB_GROUNDED forces web_search via tool_choice and verifies search
  actually occurred (ProviderSearchError if not).
- pause_turn stop_reason is treated as an incomplete search loop, not
  a successful final answer.
- provider_request_id = request-id HTTP header (support/tracking ID).
- provider_response_id = message JSON `id` (generated object ID).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.enums import LLMProvider, ProviderExecutionMode, ProviderSurface
from app.providers.base import (
    ProviderCapabilities,
    ProviderCitation,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from app.providers.errors import (
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderSearchError,
)
from app.providers.http_utils import (
    LatencyTimer,
    build_async_client,
    log_provider_result,
    map_http_error,
    map_transport_error,
    parse_json_response,
)

_PROVIDER_NAME = "Anthropic"


class AnthropicProviderAdapter:
    """Provider adapter for the Anthropic Messages API."""

    provider: LLMProvider = LLMProvider.ANTHROPIC
    surface: ProviderSurface = ProviderSurface.ANTHROPIC_MESSAGES_API

    _CAPABILITIES = ProviderCapabilities(
        supports_model_only=True,
        supports_web_grounded=True,
        supports_citations=True,
        supports_search_result_metadata=True,
    )

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport

    def __repr__(self) -> str:
        return f"<AnthropicProviderAdapter model={self._settings.anthropic_scan_model!r}>"

    def capabilities(self) -> ProviderCapabilities:
        return self._CAPABILITIES

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        # 1. Validate configuration (API key, model).
        api_key = self._settings.anthropic_api_key.get_secret_value()
        model = request.model or self._settings.anthropic_scan_model
        if not api_key:
            raise ProviderConfigurationError(
                "Anthropic API key is not configured.",
                provider=self.provider.value,
            )
        if not model:
            raise ProviderConfigurationError(
                "Anthropic scan model is not configured.",
                provider=self.provider.value,
            )

        # 2. Build request body (minimum API envelope, no system prompt distortion).
        max_tokens = (
            request.max_output_tokens
            if request.max_output_tokens is not None
            else self._settings.provider_max_output_tokens
        )
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "user", "content": request.prompt},
            ],
        }
        if request.mode == ProviderExecutionMode.WEB_GROUNDED:
            body["tools"] = [
                {
                    "type": self._settings.anthropic_web_search_tool_version,
                    "name": "web_search",
                    "max_uses": self._settings.anthropic_web_search_max_uses,
                }
            ]
            # Force the model to use the web_search tool.
            body["tool_choice"] = {"type": "tool", "name": "web_search"}

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        url = f"{self._settings.anthropic_base_url}/v1/messages"

        timer = LatencyTimer()
        client = build_async_client(settings=self._settings, transport=self._transport)
        try:
            try:
                response = await client.post(url, headers=headers, json=body)
            except Exception as exc:
                raise map_transport_error(self.provider, _PROVIDER_NAME, exc) from exc

            if response.status_code >= 400:
                map_http_error(self.provider, _PROVIDER_NAME, response)

            data = parse_json_response(self.provider, _PROVIDER_NAME, response)
        finally:
            await client.aclose()

        latency_ms = timer.elapsed_ms()

        # 3. Parse IDs: request-id header = request ID, JSON id = response ID.
        provider_request_id = response.headers.get("request-id")
        provider_response_id = data.get("id")

        content_blocks = data.get("content") or []
        if not isinstance(content_blocks, list):
            content_blocks = []

        # 4. Detect body-level web search errors.
        self._check_search_errors(content_blocks)

        # 5. Check for pause_turn — incomplete server-side search loop.
        stop_reason = data.get("stop_reason")
        if stop_reason == "pause_turn":
            raise ProviderSearchError(
                "Anthropic returned pause_turn — the server-side search loop "
                "reached its iteration limit. This is not a final answer.",
                provider=self.provider.value,
            )

        # 6. Parse response text, citations, usage, model.
        response_text = self._extract_text(content_blocks)
        citations = self._extract_citations(content_blocks)
        usage = self._extract_usage(data)
        returned_model = data.get("model")
        search_used = self._detect_search_used(content_blocks)

        # 7. Verify WEB_GROUNDED actually performed search.
        if request.mode == ProviderExecutionMode.WEB_GROUNDED and not search_used:
            raise ProviderSearchError(
                "Anthropic WEB_GROUNDED mode was requested but no web search "
                "was observed in the response.",
                provider=self.provider.value,
            )

        # 8. Validate response_text is not empty.
        if not response_text:
            raise ProviderResponseError(
                "Anthropic returned an empty response text.",
                provider=self.provider.value,
            )

        # 9. Log sanitized result.
        log_provider_result(
            provider=self.provider,
            surface=self.surface.value,
            execution_mode=request.mode.value,
            requested_model=model,
            returned_model=returned_model,
            provider_request_id=provider_request_id,
            status="ok",
            latency_ms=latency_ms,
            usage_input_tokens=usage.input_tokens,
            usage_output_tokens=usage.output_tokens,
            search_requests=usage.search_requests,
            correlation_id=request.correlation_id,
            provider_response_id=provider_response_id,
        )

        # 10. Return ProviderResult.
        return ProviderResult(
            provider=self.provider,
            surface=self.surface,
            execution_mode=request.mode,
            requested_model=model,
            returned_model=returned_model,
            response_text=response_text,
            citations=citations,
            usage=usage,
            provider_request_id=provider_request_id,
            provider_response_id=provider_response_id,
            finish_reason=stop_reason,
            latency_ms=latency_ms,
            search_used=search_used,
            metadata={},
        )

    @staticmethod
    def _check_search_errors(content_blocks: list[Any]) -> None:
        """Detect body-level web search tool errors."""
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "web_search_tool_result":
                continue
            inner = block.get("content")
            if isinstance(inner, dict) and inner.get("type") == "web_search_tool_result_error":
                error_code = inner.get("error_code", "unknown")
                raise ProviderSearchError(
                    f"Anthropic web search tool failed: {error_code}.",
                    provider=LLMProvider.ANTHROPIC.value,
                )

    @staticmethod
    def _extract_text(content_blocks: list[Any]) -> str:
        """Concatenate all text blocks' text field."""
        parts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    @staticmethod
    def _extract_citations(content_blocks: list[Any]) -> tuple[ProviderCitation, ...]:
        """Extract citations from text blocks.

        From text blocks that have a "citations" array, extract url, title,
        cited_text from each web_search_result_location citation. Deduplicate
        by URL preserving deterministic order.
        """
        seen_urls: set[str] = set()
        citations: list[ProviderCitation] = []
        for block in content_blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            cites = block.get("citations")
            if not isinstance(cites, list):
                continue
            for cite in cites:
                if not isinstance(cite, dict):
                    continue
                if cite.get("type") != "web_search_result_location":
                    continue
                url = cite.get("url")
                if not isinstance(url, str) or not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                citations.append(
                    ProviderCitation(
                        url=url,
                        title=cite.get("title"),
                        cited_text=cite.get("cited_text"),
                        source_type="web_search_result_location",
                    )
                )
        return tuple(citations)

    @staticmethod
    def _extract_usage(data: dict[str, Any]) -> ProviderUsage:
        """Extract normalized usage from the response.

        input_tokens, output_tokens from usage.
        cache_read_input_tokens → cached_input_tokens.
        cache_creation_input_tokens → cache_write_input_tokens.
        search_requests from usage.server_tool_use.web_search_requests if present.
        """
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return ProviderUsage()
        input_tokens = _safe_int(usage.get("input_tokens"))
        output_tokens = _safe_int(usage.get("output_tokens"))
        cached_input_tokens = _safe_int(usage.get("cache_read_input_tokens"))
        cache_write_input_tokens = _safe_int(usage.get("cache_creation_input_tokens"))
        search_requests: int | None = None
        server_tool_use = usage.get("server_tool_use")
        if isinstance(server_tool_use, dict):
            search_requests = _safe_int(server_tool_use.get("web_search_requests"))
        return ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_input_tokens=cache_write_input_tokens,
            search_requests=search_requests,
        )

    @staticmethod
    def _detect_search_used(content_blocks: list[Any]) -> bool:
        """Check if web search was used.

        True if any content block has type="server_tool_use" with
        name="web_search", or if there's a web_search_tool_result block.
        """
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "server_tool_use" and block.get("name") == "web_search":
                return True
            if btype == "web_search_tool_result":
                return True
        return False


def _safe_int(value: Any) -> int | None:
    """Safely convert a value to int, returning None if not an int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


__all__ = ["AnthropicProviderAdapter"]
