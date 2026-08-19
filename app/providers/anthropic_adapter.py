"""Anthropic Messages API adapter.

Implements the ProviderAdapter protocol against the Anthropic Messages API
(POST /v1/messages). This adapter is a thin infrastructure service:

- Exactly ONE HTTP request per execute() call (no automatic retries).
- No quota/usage accounting, no pricing, no persistence.
- No system prompt distortion: only the minimum API envelope is sent.
- Does not parse chain-of-thought/reasoning blocks.
- API key never appears in repr, logs, or exceptions.
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

            data = response.json()
        finally:
            await client.aclose()

        latency_ms = timer.elapsed_ms()

        # 5. Detect body-level web search errors.
        content_blocks = data.get("content") or []
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "web_search_tool_result":
                    continue
                inner = block.get("content")
                # Error case: content is a dict (not a list) with
                # type="web_search_tool_result_error".
                if isinstance(inner, dict) and inner.get("type") == "web_search_tool_result_error":
                    error_code = inner.get("error_code", "unknown")
                    raise ProviderSearchError(
                        f"Anthropic web search tool failed: {error_code}.",
                        provider=self.provider.value,
                    )

        # 6. Parse response text, citations, usage, model, request ID.
        response_text = self._extract_text(content_blocks)
        citations = self._extract_citations(content_blocks)
        usage = self._extract_usage(data)
        returned_model = data.get("model")
        provider_request_id = data.get("id")
        finish_reason = data.get("stop_reason")
        search_used = self._detect_search_used(content_blocks)

        # 7. Validate response_text is not empty.
        if not response_text:
            raise ProviderResponseError(
                "Anthropic returned an empty response text.",
                provider=self.provider.value,
            )

        # 8. Log sanitized result.
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
        )

        # 9. Return ProviderResult.
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
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            search_used=search_used,
            metadata={},
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

        input_tokens, output_tokens from usage. search_requests from
        usage.server_tool_use.web_search_requests if present.
        """
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return ProviderUsage()
        raw_input = usage.get("input_tokens")
        raw_output = usage.get("output_tokens")
        input_tokens = raw_input if isinstance(raw_input, int) else None
        output_tokens = raw_output if isinstance(raw_output, int) else None
        search_requests: int | None = None
        server_tool_use = usage.get("server_tool_use")
        if isinstance(server_tool_use, dict):
            web_search_requests = server_tool_use.get("web_search_requests")
            if isinstance(web_search_requests, int):
                search_requests = web_search_requests
        return ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
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


# Make the adapter structurally compatible with the ProviderAdapter protocol.
# The protocol is runtime_checkable; the class already implements the required
# attributes/methods, so no explicit inheritance is needed.
__all__ = ["AnthropicProviderAdapter"]
