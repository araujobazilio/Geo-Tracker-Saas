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
"""

from __future__ import annotations

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
)
from app.providers.http_utils import (
    LatencyTimer,
    build_async_client,
    log_provider_result,
    map_http_error,
    map_transport_error,
)


class OpenAIProviderAdapter:
    """Provider adapter for the OpenAI Responses API.

    Does NOT inherit from ProviderAdapter (it is a Protocol); it simply
    implements the expected methods and attributes.
    """

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
        body: dict[str, object] = {
            "model": model,
            "input": request.prompt,
            "store": False,
        }
        if request.mode == ProviderExecutionMode.WEB_GROUNDED:
            body["tools"] = [{"type": "web_search"}]

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
                raise map_transport_error(LLMProvider.OPENAI, "OpenAI", exc) from exc

            # 4. Map HTTP errors.
            if response.status_code >= 400:
                map_http_error(LLMProvider.OPENAI, "OpenAI", response)

            data = response.json()
        finally:
            await client.aclose()

        # 5. Parse response.
        provider_request_id = data.get("id") or response.headers.get("x-request-id")
        returned_model = data.get("model")

        # output_text convenience field, or extract from output array.
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

        # 6. Validate response_text is not empty.
        if not output_text or not output_text.strip():
            raise ProviderResponseError(
                "OpenAI returned an empty response text.",
                provider=LLMProvider.OPENAI.value,
            )

        # Citations: look through output items for type="message", then content
        # array for type="output_text", then annotations for type="url_citation".
        raw_citations: list[ProviderCitation] = []
        for item in data.get("output", []) or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") != "output_text":
                    continue
                for ann in content.get("annotations", []) or []:
                    if not isinstance(ann, dict):
                        continue
                    if ann.get("type") != "url_citation":
                        continue
                    url_val: str | None = ann.get("url")
                    if not url_val:
                        continue
                    url = url_val
                    raw_citations.append(
                        ProviderCitation(
                            url=url,
                            title=ann.get("title"),
                            source_type="url_citation",
                            start_index=ann.get("start_index"),
                            end_index=ann.get("end_index"),
                        )
                    )

        # 7. Normalize citations: deduplicate URLs preserving deterministic order.
        seen_urls: set[str] = set()
        citations: list[ProviderCitation] = []
        for cite in raw_citations:
            if cite.url in seen_urls:
                continue
            seen_urls.add(cite.url)
            citations.append(cite)

        # Usage parsing.
        usage_data = data.get("usage") or {}
        usage = ProviderUsage(
            input_tokens=usage_data.get("input_tokens"),
            output_tokens=usage_data.get("output_tokens"),
            total_tokens=usage_data.get("total_tokens"),
        )

        # search_used: check if any output item has type="web_search_call".
        search_used = any(
            isinstance(item, dict) and item.get("type") == "web_search_call"
            for item in (data.get("output", []) or [])
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
            finish_reason=None,
            latency_ms=latency_ms,
            search_used=search_used,
            metadata={},
        )

        # 8. Log sanitized result.
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
            search_requests=None,
            correlation_id=request.correlation_id,
        )

        # 9. Return ProviderResult.
        return result
