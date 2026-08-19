"""Google Gemini API adapter (MODEL_ONLY).

Implements the ProviderAdapter protocol for the Google Gemini API
(POST {base_url}/v1beta/models/{model}:generateContent).

Key design decisions:
- Exactly ONE HTTP request per execute() — no automatic retries.
- No quota/usage calls, no UsageEvent creation, no pricing.
- No system prompt distortion — only the minimum API envelope.
- API key never appears in repr, logs, or exceptions.

COMPLIANCE RESTRICTION:
- WEB_GROUNDED mode is NOT supported and must fail BEFORE any network
  call. Google Search grounding terms conflict with GEO Tracker's
  planned automated storage and analysis.
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


class GoogleProviderAdapter:
    """Provider adapter for the Google Gemini API (MODEL_ONLY).

    Does NOT inherit from ProviderAdapter (it is a Protocol); it simply
    implements the expected methods and attributes.
    """

    provider: LLMProvider = LLMProvider.GOOGLE
    surface: ProviderSurface = ProviderSurface.GOOGLE_GEMINI_API

    _CAPABILITIES = ProviderCapabilities(
        supports_model_only=True,
        supports_web_grounded=False,
        supports_citations=False,
        supports_search_result_metadata=False,
    )

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport

    def __repr__(self) -> str:
        return f"<GoogleProviderAdapter model={self._settings.google_scan_model!r}>"

    def capabilities(self) -> ProviderCapabilities:
        return self._CAPABILITIES

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        # 1. WEB_GROUNDED must fail BEFORE any network call (compliance).
        if request.mode == ProviderExecutionMode.WEB_GROUNDED:
            raise ProviderModeNotAllowedError(
                "Google Search grounding is disabled for this provider surface.",
                provider=LLMProvider.GOOGLE.value,
            )

        # 2. Validate configuration.
        api_key = (
            self._settings.google_api_key.get_secret_value()
            if self._settings.google_api_key
            else ""
        )
        model = request.model or self._settings.google_scan_model
        if not api_key:
            raise ProviderConfigurationError(
                "Google API key is not configured.",
                provider=LLMProvider.GOOGLE.value,
            )
        if not model:
            raise ProviderConfigurationError(
                "Google scan model is not configured.",
                provider=LLMProvider.GOOGLE.value,
            )

        # 3. Build request URL and body — minimum API envelope, no system prompt.
        base_url = self._settings.google_base_url.rstrip("/")
        url = f"{base_url}/v1beta/models/{model}:generateContent"
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }
        body: dict[str, object] = {
            "contents": [
                {"parts": [{"text": request.prompt}]},
            ],
        }

        timer = LatencyTimer()

        # 4. Make exactly ONE httpx POST request.
        client = build_async_client(settings=self._settings, transport=self._transport)
        try:
            try:
                response = await client.post(url, headers=headers, json=body)
            except Exception as exc:
                raise map_transport_error(LLMProvider.GOOGLE, "Google", exc) from exc

            # 5. Map HTTP errors.
            if response.status_code >= 400:
                map_http_error(LLMProvider.GOOGLE, "Google", response)

            data = response.json()
        finally:
            await client.aclose()

        # 6. Parse response text — concatenate all parts text.
        response_text = ""
        candidates = data.get("candidates") or []
        if candidates and isinstance(candidates[0], dict):
            content = candidates[0].get("content") or {}
            parts: list[str] = []
            for part in content.get("parts", []) or []:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            response_text = "".join(parts)

        # 7. Validate response_text is not empty.
        if not response_text or not response_text.strip():
            raise ProviderResponseError(
                "Google returned an empty response text.",
                provider=LLMProvider.GOOGLE.value,
            )

        # 8. Parse usage metadata.
        usage_data = data.get("usageMetadata") or {}
        usage = ProviderUsage(
            input_tokens=usage_data.get("promptTokenCount"),
            output_tokens=usage_data.get("candidatesTokenCount"),
            total_tokens=usage_data.get("totalTokenCount"),
        )

        # 9. Parse returned model and finish reason.
        returned_model = data.get("modelVersion")
        finish_reason = None
        if candidates and isinstance(candidates[0], dict):
            finish_reason = candidates[0].get("finishReason")

        # Google does not provide a standard request ID in the response body.
        provider_request_id = None

        # MODEL_ONLY only — no citations, no search.
        citations: tuple[ProviderCitation, ...] = ()
        search_used = False

        latency_ms = timer.elapsed_ms()

        result = ProviderResult(
            provider=LLMProvider.GOOGLE,
            surface=ProviderSurface.GOOGLE_GEMINI_API,
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

        # 10. Log sanitized result.
        log_provider_result(
            provider=LLMProvider.GOOGLE,
            surface=ProviderSurface.GOOGLE_GEMINI_API.value,
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

        return result
