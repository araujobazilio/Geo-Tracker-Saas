"""Perplexity Sonar API adapter (WEB_GROUNDED only).

This adapter talks to the Perplexity Sonar chat completions endpoint.
Sonar is web-grounded only — MODEL_ONLY is rejected before any network
call.

Key invariants:
- Exactly ONE HTTP request per execute() (no automatic retries).
- No quota calls, no UsageEvent creation, no pricing.
- No system prompt distortion — only the minimum API envelope.
- API key never appears in repr, logs, or exceptions.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.enums import LLMProvider, ProviderExecutionMode, ProviderSurface
from app.providers.base import (
    ProviderAdapter,
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

_PROVIDER_NAME = "Perplexity Sonar"


class PerplexityProviderAdapter:
    """Provider adapter for the Perplexity Sonar API (WEB_GROUNDED only)."""

    provider: LLMProvider = LLMProvider.PERPLEXITY
    surface: ProviderSurface = ProviderSurface.PERPLEXITY_SONAR_API

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_model_only=False,
            supports_web_grounded=True,
            supports_citations=True,
            supports_search_result_metadata=True,
        )

    def __repr__(self) -> str:
        return f"<PerplexityProviderAdapter model={self._settings.perplexity_scan_model!r}>"

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        # 1. Mode guard — BEFORE any network call.
        if request.mode == ProviderExecutionMode.MODEL_ONLY:
            raise ProviderModeNotAllowedError(
                "Perplexity Sonar supports WEB_GROUNDED only.",
                provider="PERPLEXITY",
            )

        # 2. Validate configuration.
        api_key = (
            self._settings.perplexity_api_key.get_secret_value()
            if self._settings.perplexity_api_key
            else ""
        )
        model = request.model or self._settings.perplexity_scan_model
        if not api_key:
            raise ProviderConfigurationError(
                "Perplexity API key is not configured.",
                provider="PERPLEXITY",
            )
        if not model:
            raise ProviderConfigurationError(
                "Perplexity scan model is not configured.",
                provider="PERPLEXITY",
            )

        base_url = self._settings.perplexity_base_url.rstrip("/")
        url = f"{base_url}/v1/sonar"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "messages": [{"role": "user", "content": request.prompt}],
        }

        timer = LatencyTimer()

        # 4. Exactly ONE HTTP request.
        try:
            async with build_async_client(
                settings=self._settings, transport=self._transport
            ) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise map_transport_error(LLMProvider.PERPLEXITY, _PROVIDER_NAME, exc) from exc

        # 5. Map HTTP errors.
        if response.status_code >= 400:
            map_http_error(LLMProvider.PERPLEXITY, _PROVIDER_NAME, response)

        latency_ms = timer.elapsed_ms()

        # 6. Parse response.
        try:
            data = response.json()
        except Exception as exc:
            raise ProviderResponseError(
                "Perplexity returned an unparseable response body.",
                provider="PERPLEXITY",
            ) from exc

        choices = data.get("choices") or []
        response_text = ""
        finish_reason: str | None = None
        if choices:
            first = choices[0]
            message = first.get("message") or {}
            response_text = message.get("content") or ""
            finish_reason = first.get("finish_reason")

        # 7. Validate response text.
        if not response_text:
            raise ProviderResponseError(
                "Perplexity returned an empty response text.",
                provider="PERPLEXITY",
            )

        citations = self._parse_citations(data)

        usage_data = data.get("usage") or {}
        usage = ProviderUsage(
            input_tokens=usage_data.get("prompt_tokens"),
            output_tokens=usage_data.get("completion_tokens"),
            total_tokens=usage_data.get("total_tokens"),
            search_requests=usage_data.get("num_search_queries"),
        )

        returned_model = data.get("model")
        provider_request_id = data.get("id")

        result = ProviderResult(
            provider=LLMProvider.PERPLEXITY,
            surface=ProviderSurface.PERPLEXITY_SONAR_API,
            execution_mode=request.mode,
            requested_model=model,
            returned_model=returned_model,
            response_text=response_text,
            citations=citations,
            usage=usage,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            search_used=True,
            metadata={},
        )

        # 8. Log sanitized result.
        log_provider_result(
            provider=LLMProvider.PERPLEXITY,
            surface=ProviderSurface.PERPLEXITY_SONAR_API.value,
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

        return result

    @staticmethod
    def _parse_citations(data: dict[str, Any]) -> tuple[ProviderCitation, ...]:
        """Normalize citations from search_results (primary) with
        fallback to the legacy citations URL array.

        Deduplicates by URL preserving deterministic first-seen order.
        """
        seen: set[str] = set()
        out: list[ProviderCitation] = []

        search_results = data.get("search_results")
        if search_results:
            for item in search_results:
                url = (item.get("url") or "").strip() if isinstance(item, dict) else ""
                if not url or url in seen:
                    continue
                seen.add(url)
                out.append(
                    ProviderCitation(
                        url=url,
                        title=item.get("title"),
                        source_type=item.get("source"),
                    )
                )
            if out:
                return tuple(out)

        # Legacy fallback: array of URL strings.
        legacy = data.get("citations")
        if legacy:
            for url in legacy:
                url = (url or "").strip() if isinstance(url, str) else ""
                if not url or url in seen:
                    continue
                seen.add(url)
                out.append(ProviderCitation(url=url, title=None))

        return tuple(out)


# Satisfy static type checkers for the ProviderAdapter Protocol.
_: ProviderAdapter = PerplexityProviderAdapter  # type: ignore[assignment]
