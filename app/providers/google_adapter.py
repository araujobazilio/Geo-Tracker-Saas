"""Google Gemini Interactions API adapter (MODEL_ONLY).

Implements the ProviderAdapter protocol for the Google Gemini Interactions API
(POST {base_url}/v1/interactions).

Key design decisions:
- Exactly ONE HTTP request per execute() — no automatic retries.
- No quota/usage calls, no UsageEvent creation, no pricing.
- No system prompt distortion — only the minimum API envelope.
- store=false for every interaction (stateless one-shot measurement).
- Does NOT parse chain-of-thought/thought steps — only final model output.
- API key never appears in repr, logs, or exceptions.

COMPLIANCE RESTRICTION:
- WEB_GROUNDED mode is NOT supported and must fail BEFORE any network
  call. Google Search grounding terms conflict with GEO Tracker's
  planned automated storage and analysis.
- This measures GOOGLE_INTERACTIONS_API MODEL_ONLY, NOT Google AI Overviews.
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
)
from app.providers.http_utils import (
    LatencyTimer,
    build_async_client,
    log_provider_result,
    map_http_error,
    map_transport_error,
    parse_json_response,
)

_PROVIDER_NAME = "Google"

# Status values that indicate the interaction did NOT complete successfully.
_INCOMPLETE_STATUSES = {"failed", "cancelled", "requires_action", "incomplete", "budget_exceeded"}


class GoogleProviderAdapter:
    """Provider adapter for the Google Gemini Interactions API (MODEL_ONLY)."""

    provider: LLMProvider = LLMProvider.GOOGLE
    surface: ProviderSurface = ProviderSurface.GOOGLE_INTERACTIONS_API

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

        # 3. Build Interactions API request — minimum envelope, no system instruction.
        max_output_tokens = (
            request.max_output_tokens
            if request.max_output_tokens is not None
            else self._settings.provider_max_output_tokens
        )
        base_url = self._settings.google_base_url.rstrip("/")
        url = f"{base_url}/v1/interactions"
        headers = {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": model,
            "input": request.prompt,
            "store": False,
            "generation_config": {
                "max_output_tokens": max_output_tokens,
            },
        }

        timer = LatencyTimer()

        # 4. Make exactly ONE httpx POST request.
        client = build_async_client(settings=self._settings, transport=self._transport)
        try:
            try:
                response = await client.post(url, headers=headers, json=body)
            except Exception as exc:
                raise map_transport_error(LLMProvider.GOOGLE, _PROVIDER_NAME, exc) from exc

            # 5. Map HTTP errors.
            if response.status_code >= 400:
                map_http_error(LLMProvider.GOOGLE, _PROVIDER_NAME, response)

            data = parse_json_response(LLMProvider.GOOGLE, _PROVIDER_NAME, response)
        finally:
            await client.aclose()

        # 6. Parse interaction ID and status.
        provider_response_id = data.get("id")
        status = data.get("status")

        # 7. Check status — do not silently classify incomplete as success.
        if status and status in _INCOMPLETE_STATUSES:
            raise ProviderResponseError(
                f"Google interaction did not complete successfully (status={status}).",
                provider=LLMProvider.GOOGLE.value,
            )

        # 8. Parse final output text from steps — only model_output steps.
        #    Discard thought/reasoning steps entirely.
        response_text = self._extract_output_text(data)

        # 9. Validate response_text is not empty.
        if not response_text or not response_text.strip():
            raise ProviderResponseError(
                "Google returned an empty response text.",
                provider=LLMProvider.GOOGLE.value,
            )

        # 10. Parse usage metadata.
        usage_data = data.get("usage") or {}
        usage = ProviderUsage(
            input_tokens=_safe_int(usage_data.get("total_input_tokens")),
            output_tokens=_safe_int(usage_data.get("total_output_tokens")),
            total_tokens=_safe_int(usage_data.get("total_tokens")),
            cached_input_tokens=_safe_int(usage_data.get("total_cached_tokens")),
            reasoning_tokens=_safe_int(usage_data.get("total_thought_tokens")),
        )

        returned_model = data.get("model")

        # Google does not provide a standard HTTP request ID header.
        provider_request_id = None

        # MODEL_ONLY only — no citations, no search.
        citations: tuple[ProviderCitation, ...] = ()
        search_used = False

        latency_ms = timer.elapsed_ms()

        result = ProviderResult(
            provider=LLMProvider.GOOGLE,
            surface=ProviderSurface.GOOGLE_INTERACTIONS_API,
            execution_mode=request.mode,
            requested_model=model,
            returned_model=returned_model,
            response_text=response_text,
            citations=citations,
            usage=usage,
            provider_request_id=provider_request_id,
            provider_response_id=provider_response_id,
            finish_reason=status,
            latency_ms=latency_ms,
            search_used=search_used,
            metadata={},
        )

        # 11. Log sanitized result.
        log_provider_result(
            provider=LLMProvider.GOOGLE,
            surface=ProviderSurface.GOOGLE_INTERACTIONS_API.value,
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
            provider_response_id=provider_response_id,
        )

        return result

    @staticmethod
    def _extract_output_text(data: dict[str, Any]) -> str:
        """Extract final output text from model_output steps.

        Only steps with type="model_output" are parsed for text.
        Thought/reasoning steps are discarded entirely — their content
        is never exposed in ProviderResult.
        """
        parts: list[str] = []
        steps = data.get("steps") or []
        if not isinstance(steps, list):
            return ""
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("type") != "model_output":
                continue
            content = step.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        return "".join(parts)


def _safe_int(value: Any) -> int | None:
    """Safely convert a value to int, returning None if not an int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
