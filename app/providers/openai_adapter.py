"""OpenAI Responses API adapter.

Implements the ProviderAdapter protocol for the OpenAI Responses API
(POST {base_url}/responses).

Key design decisions:
- Exactly ONE HTTP request per execute() — no automatic retries.
- No quota/usage calls, no UsageEvent creation, no pricing.
- store=false in all requests (privacy/reproducibility).
- No system prompt distortion — only the minimum API envelope.
- Does NOT parse chain-of-thought/reasoning blocks.
- API key never appears in repr, logs, or exceptions.
- WEB_GROUNDED forces web_search via tool_choice and verifies search
  actually occurred (ProviderSearchError if not).
- provider_request_id = x-request-id HTTP header (support/tracking ID).
- provider_response_id = response JSON `id` (generated object ID).
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
    ProviderModeNotAllowedError,
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

_PROVIDER_NAME = "OpenAI"


class OpenAIProviderAdapter:
    """Provider adapter for the OpenAI Responses API."""

    provider: LLMProvider = LLMProvider.OPENAI
    surface: ProviderSurface = ProviderSurface.OPENAI_RESPONSES_API

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
        return f"<OpenAIProviderAdapter model={self._settings.openai_scan_model!r}>"

    def capabilities(self) -> ProviderCapabilities:
        return self._CAPABILITIES

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        # 1. Validate configuration.
        api_key = (
            self._settings.openai_api_key.get_secret_value()
            if self._settings.openai_api_key
            else ""
        )
        model = request.model or self._settings.openai_scan_model
        if not api_key:
            raise ProviderConfigurationError(
                "OpenAI API key is not configured.",
                provider=LLMProvider.OPENAI.value,
            )
        if not model:
            raise ProviderConfigurationError(
                "OpenAI scan model is not configured.",
                provider=LLMProvider.OPENAI.value,
            )

        # Validate mode is supported.
        caps = self._CAPABILITIES
        if request.mode == ProviderExecutionMode.MODEL_ONLY and not caps.supports_model_only:
            raise ProviderModeNotAllowedError(
                "OpenAI does not support MODEL_ONLY mode.",
                provider=LLMProvider.OPENAI.value,
            )
        if request.mode == ProviderExecutionMode.WEB_GROUNDED and not caps.supports_web_grounded:
            raise ProviderModeNotAllowedError(
                "OpenAI does not support WEB_GROUNDED mode.",
                provider=LLMProvider.OPENAI.value,
            )

        # 2. Build request body — minimum API envelope, no system prompt.
        max_output_tokens = (
            request.max_output_tokens
            if request.max_output_tokens is not None
            else self._settings.provider_max_output_tokens
        )
        body: dict[str, Any] = {
            "model": model,
            "input": request.prompt,
            "store": False,
            "max_output_tokens": max_output_tokens,
        }
        if request.mode == ProviderExecutionMode.WEB_GROUNDED:
            body["tools"] = [{"type": "web_search"}]
            # Force the model to use the web_search tool.
            body["tool_choice"] = {"type": "web_search"}
            # Request broader source metadata.
            body["include"] = ["web_search_call.action.sources"]

        base_url = self._settings.openai_base_url.rstrip("/")
        url = f"{base_url}/responses"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        timer = LatencyTimer()

        # 3. Make exactly ONE httpx POST request.
        client = build_async_client(settings=self._settings, transport=self._transport)
        try:
            try:
                response = await client.post(url, headers=headers, json=body)
            except Exception as exc:
                raise map_transport_error(LLMProvider.OPENAI, _PROVIDER_NAME, exc) from exc

            # 4. Map HTTP errors.
            if response.status_code >= 400:
                map_http_error(LLMProvider.OPENAI, _PROVIDER_NAME, response)

            data = parse_json_response(LLMProvider.OPENAI, _PROVIDER_NAME, response)
        finally:
            await client.aclose()

        # 5. Parse IDs: x-request-id header = request ID, JSON id = response ID.
        provider_request_id = response.headers.get("x-request-id")
        provider_response_id = data.get("id")
        returned_model = data.get("model")

        # 6. Parse output text.
        output_text = data.get("output_text")
        if not output_text:
            parts: list[str] = []
            for item in data.get("output", []) or []:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for content in item.get("content", []) or []:
                    if not isinstance(content, dict):
                        continue
                    if content.get("type") == "output_text":
                        text = content.get("text")
                        if isinstance(text, str):
                            parts.append(text)
            output_text = "".join(parts)

        # 7. Validate response_text is not empty.
        if not output_text or not output_text.strip():
            raise ProviderResponseError(
                "OpenAI returned an empty response text.",
                provider=LLMProvider.OPENAI.value,
            )

        # 8. Parse citations from inline url_citation annotations AND
        #    web_search_call.action.sources. Deduplicate by URL.
        raw_citations: list[ProviderCitation] = []
        for item in data.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            # Inline url_citation annotations from message content.
            if item.get("type") == "message":
                for content in item.get("content", []) or []:
                    if not isinstance(content, dict) or content.get("type") != "output_text":
                        continue
                    for ann in content.get("annotations", []) or []:
                        if not isinstance(ann, dict) or ann.get("type") != "url_citation":
                            continue
                        url_val: str | None = ann.get("url")
                        if not url_val:
                            continue
                        raw_citations.append(
                            ProviderCitation(
                                url=url_val,
                                title=ann.get("title"),
                                source_type="url_citation",
                                start_index=ann.get("start_index"),
                                end_index=ann.get("end_index"),
                            )
                        )
            # Source URLs from web_search_call.action.sources.
            if item.get("type") == "web_search_call":
                action = item.get("action")
                if isinstance(action, dict):
                    for src in action.get("sources", []) or []:
                        if not isinstance(src, dict):
                            continue
                        src_url = src.get("url")
                        if not isinstance(src_url, str) or not src_url:
                            continue
                        raw_citations.append(
                            ProviderCitation(
                                url=src_url,
                                title=None,
                                source_type="web_search_source",
                            )
                        )

        # Deduplicate by URL preserving deterministic first-seen order.
        seen_urls: set[str] = set()
        citations: list[ProviderCitation] = []
        for cite in raw_citations:
            if cite.url in seen_urls:
                continue
            seen_urls.add(cite.url)
            citations.append(cite)

        # 9. Parse usage with all available fields.
        usage_data = data.get("usage") or {}
        input_tokens = _safe_int(usage_data.get("input_tokens"))
        output_tokens = _safe_int(usage_data.get("output_tokens"))
        total_tokens = _safe_int(usage_data.get("total_tokens"))
        cached_input_tokens = None
        reasoning_tokens = None
        input_details = usage_data.get("input_tokens_details")
        if isinstance(input_details, dict):
            cached_input_tokens = _safe_int(input_details.get("cached_tokens"))
        output_details = usage_data.get("output_tokens_details")
        if isinstance(output_details, dict):
            reasoning_tokens = _safe_int(output_details.get("reasoning_tokens"))

        # Count web_search_call items for search_requests.
        web_search_calls = [
            item
            for item in (data.get("output", []) or [])
            if isinstance(item, dict) and item.get("type") == "web_search_call"
        ]
        search_requests = len(web_search_calls) if web_search_calls else None

        usage = ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=cached_input_tokens,
            reasoning_tokens=reasoning_tokens,
            search_requests=search_requests,
        )

        # 10. Verify WEB_GROUNDED actually performed search.
        search_used = bool(web_search_calls)
        if request.mode == ProviderExecutionMode.WEB_GROUNDED and not search_used:
            raise ProviderSearchError(
                "OpenAI WEB_GROUNDED mode was requested but no web search call "
                "was observed in the response.",
                provider=LLMProvider.OPENAI.value,
            )

        latency_ms = timer.elapsed_ms()

        result = ProviderResult(
            provider=LLMProvider.OPENAI,
            surface=ProviderSurface.OPENAI_RESPONSES_API,
            execution_mode=request.mode,
            requested_model=model,
            returned_model=returned_model,
            response_text=output_text,
            citations=tuple(citations),
            usage=usage,
            provider_request_id=provider_request_id,
            provider_response_id=provider_response_id,
            finish_reason=None,
            latency_ms=latency_ms,
            search_used=search_used,
            metadata={},
        )

        # 11. Log sanitized result.
        log_provider_result(
            provider=LLMProvider.OPENAI,
            surface=ProviderSurface.OPENAI_RESPONSES_API.value,
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

        return result


def _safe_int(value: Any) -> int | None:
    """Safely convert a value to int, returning None if not an int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
