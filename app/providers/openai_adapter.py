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
- Web tool evidence is split into explicit counters:
    web_tool_call_count        (TOTAL web_search_call items — bound authority)
    search_action_count        (action.type == "search" — billing authority)
    open_page_action_count     (action.type == "open_page")
    find_in_page_action_count  (action.type == "find_in_page")
    unknown_web_action_count   (missing/unrecognized action — fail-closed)
  Legacy ``search_requests`` == web_tool_call_count for compatibility.
- max_tool_calls is enforced against web_tool_call_count (total), never
  against search_action_count.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.enums import LLMProvider, ProviderExecutionMode, ProviderSurface
from app.core.logging import get_logger
from app.providers.base import (
    ProviderCapabilities,
    ProviderCitation,
    ProviderFailureEvidence,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from app.providers.errors import (
    ProviderConfigurationError,
    ProviderContractViolationError,
    ProviderModeNotAllowedError,
    ProviderResponseError,
    ProviderSearchError,
)
from app.providers.evidence import ProviderEvidenceSink
from app.providers.http_utils import (
    LatencyTimer,
    build_async_client,
    log_provider_result,
    map_http_error,
    map_transport_error,
    parse_json_response,
)
from app.providers.web_actions import classify_openai_web_actions

_PROVIDER_NAME = "OpenAI"
logger = get_logger("app.providers.openai")


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
        evidence_sink: ProviderEvidenceSink | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport
        self._evidence_sink = evidence_sink

    def __repr__(self) -> str:
        return f"<OpenAIProviderAdapter model={self._settings.openai_scan_model!r}>"

    def with_evidence_sink(
        self,
        evidence_sink: ProviderEvidenceSink,
        *,
        settings: Settings | None = None,
    ) -> OpenAIProviderAdapter:
        """Return an equivalent adapter instrumented for one validation."""

        return OpenAIProviderAdapter(
            settings=settings or self._settings,
            transport=self._transport,
            evidence_sink=evidence_sink,
        )

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
            # Force execution of the only configured tool: web_search.
            body["tool_choice"] = "required"
            body["max_tool_calls"] = self._settings.openai_web_search_max_tool_calls
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

            # Capture transport facts before attempting JSON parsing.  The
            # sink receives only a body fingerprint/length and the non-secret
            # request ID; Authorization is deliberately excluded.
            if self._evidence_sink is not None:
                provider_request_id_for_evidence = response.headers.get("x-request-id")
                try:
                    self._evidence_sink.capture_transport(
                        response_status=response.status_code,
                        response_headers={"x-request-id": provider_request_id_for_evidence or ""},
                        response_bytes=response.content,
                        provider_request_id=provider_request_id_for_evidence,
                        latency_ms=timer.elapsed_ms(),
                    )
                except Exception:
                    logger.warning(
                        "provider_transport_evidence_capture_failed",
                        provider=LLMProvider.OPENAI.value,
                        correlation_id=request.correlation_id,
                    )

                # Persist the complete response envelope only after parsing,
                # through the sink's recursive sanitizer.  Malformed JSON is
                # represented by a sanitized parse-failure marker while the
                # transport artifact remains available for diagnosis.
                response_json: dict[str, Any] | None = None
                try:
                    parsed = response.json()
                    if isinstance(parsed, dict):
                        response_json = parsed
                except (TypeError, ValueError):
                    response_json = None
                provider_response_id_for_evidence = (
                    response_json.get("id") if response_json is not None else None
                )
                if not isinstance(provider_response_id_for_evidence, str):
                    provider_response_id_for_evidence = None
                try:
                    self._evidence_sink.capture_response(
                        request=request,
                        request_body=body,
                        response_status=response.status_code,
                        response_headers={"x-request-id": provider_request_id_for_evidence or ""},
                        response_bytes=response.content,
                        response_json=response_json,
                        provider_request_id=provider_request_id_for_evidence,
                        provider_response_id=provider_response_id_for_evidence,
                        latency_ms=timer.elapsed_ms(),
                    )
                except Exception:
                    # The provider response must still flow through the
                    # canonical accounting path.  The operator inspects the
                    # sink error and reports the run inconclusive; it never
                    # repeats the paid call.
                    logger.warning(
                        "provider_evidence_capture_failed",
                        provider=LLMProvider.OPENAI.value,
                        correlation_id=request.correlation_id,
                    )

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

        # 6. Parse incomplete_details (if present) BEFORE any validation
        #    that might raise.  This is billable evidence.
        incomplete_reason: str | None = None
        incomplete_details = data.get("incomplete_details")
        if isinstance(incomplete_details, dict):
            reason = incomplete_details.get("reason")
            if isinstance(reason, str) and reason:
                incomplete_reason = reason
        status_field = data.get("status")
        if isinstance(status_field, str) and status_field == "incomplete" and not incomplete_reason:
            incomplete_reason = "incomplete"

        # 7. Parse output text.
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
        cache_write_input_tokens = None
        reasoning_tokens = None
        input_details = usage_data.get("input_tokens_details")
        if isinstance(input_details, dict):
            cached_input_tokens = _safe_int(input_details.get("cached_tokens"))
            cache_write_input_tokens = _safe_int(input_details.get("cache_write_tokens"))
        output_details = usage_data.get("output_tokens_details")
        if isinstance(output_details, dict):
            reasoning_tokens = _safe_int(output_details.get("reasoning_tokens"))

        # 9a. Web tool evidence.  Every ``web_search_call`` output item is one
        #     built-in tool call processed by the provider.  Each item carries
        #     an ``action`` whose ``type`` is one of:
        #       - "search"        → documented billable web-search action
        #       - "open_page"     → page navigation (reasoning models)
        #       - "find_in_page"  → in-page pattern search (reasoning models)
        #     Anything else (or a missing action dict) is classified as
        #     unknown.  Only counts are extracted — never queries, URLs, or
        #     raw payloads.
        #
        #     IMPORTANT (future-proofing): this request configures exactly ONE
        #     built-in tool (web_search).  Therefore, FOR THIS REQUEST,
        #     total built-in tool calls == web_tool_call_count.  If another
        #     built-in tool is ever added to ``body["tools"]``, the
        #     max_tool_calls enforcement below MUST be generalized to count
        #     ALL built-in tool call output items, not just web_search_call.
        web_actions = classify_openai_web_actions(data.get("output"))
        web_tool_call_count = web_actions.web_tool_call_count
        search_action_count = web_actions.search_action_count
        open_page_action_count = web_actions.open_page_action_count
        find_in_page_action_count = web_actions.find_in_page_action_count
        unknown_web_action_count = web_actions.unknown_web_action_count

        # LEGACY: ``search_requests`` keeps its historical OpenAI semantics —
        # the TOTAL number of web_search_call items — so that new rows remain
        # comparable with rows written before the action breakdown existed.
        # It is NOT a billing authority; PricingService uses
        # search_action_count exclusively.
        search_requests = web_tool_call_count

        usage = ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_input_tokens=cache_write_input_tokens,
            reasoning_tokens=reasoning_tokens,
            search_requests=search_requests,
            web_tool_call_count=web_tool_call_count,
            search_action_count=search_action_count,
            open_page_action_count=open_page_action_count,
            find_in_page_action_count=find_in_page_action_count,
            unknown_web_action_count=unknown_web_action_count,
        )

        search_used = web_tool_call_count is not None

        # 9b. Detect max_tool_calls contract violation.  The OpenAI bound
        #     applies to the TOTAL number of built-in tool calls processed,
        #     so the comparison uses web_tool_call_count (all action types),
        #     NOT search_action_count.  Do NOT clamp the count — preserve the
        #     raw evidence.  This violation is raised even when response_text
        #     is valid, because the provider did not respect the bounds of
        #     the execution request.
        configured_max_tool_calls = self._settings.openai_web_search_max_tool_calls
        max_tool_calls_violation: int | None = None
        if (
            request.mode == ProviderExecutionMode.WEB_GROUNDED
            and web_tool_call_count is not None
            and web_tool_call_count > configured_max_tool_calls
        ):
            max_tool_calls_violation = web_tool_call_count

        latency_ms = timer.elapsed_ms()

        # 10. Build failure evidence for responses that have a valid envelope
        #     but are NOT functionally usable.  This must happen BEFORE raising
        #     so the exception carries billable evidence.
        def _build_evidence() -> ProviderFailureEvidence:
            return ProviderFailureEvidence(
                provider=LLMProvider.OPENAI,
                surface=ProviderSurface.OPENAI_RESPONSES_API,
                execution_mode=request.mode,
                requested_model=model,
                returned_model=returned_model,
                provider_request_id=provider_request_id,
                provider_response_id=provider_response_id,
                usage=usage,
                citations=tuple(citations),
                latency_ms=latency_ms,
                search_used=search_used,
                incomplete_reason=incomplete_reason,
                max_tool_calls_violation=max_tool_calls_violation,
                requested_max_tool_calls=(
                    configured_max_tool_calls
                    if request.mode == ProviderExecutionMode.WEB_GROUNDED
                    else None
                ),
                observed_search_requests=search_requests,
                observed_web_tool_call_count=web_tool_call_count,
                search_action_count=search_action_count,
                open_page_action_count=open_page_action_count,
                find_in_page_action_count=find_in_page_action_count,
                unknown_web_action_count=unknown_web_action_count,
            )

        # 11. An incomplete response is billable evidence but is not a
        #     successful validation, even if the provider returned partial
        #     text.  Preserve the envelope through ProviderFailureEvidence.
        if incomplete_reason is not None:
            raise ProviderResponseError(
                "OpenAI returned an incomplete response.",
                provider=LLMProvider.OPENAI.value,
                evidence=_build_evidence(),
            )

        # 12. Validate response_text is not empty.
        if not output_text or not output_text.strip():
            raise ProviderResponseError(
                "OpenAI returned an empty response text.",
                provider=LLMProvider.OPENAI.value,
                evidence=_build_evidence(),
            )

        # 13. Verify WEB_GROUNDED actually performed search.
        if request.mode == ProviderExecutionMode.WEB_GROUNDED and not search_used:
            raise ProviderSearchError(
                "OpenAI WEB_GROUNDED mode was requested but no web search call "
                "was observed in the response.",
                provider=LLMProvider.OPENAI.value,
                evidence=_build_evidence(),
            )

        # 13b. Verify WEB_GROUNDED did not exceed the configured max_tool_calls.
        #      This is a provider contract violation — the provider returned a
        #      billable response but did not respect the execution bounds.
        #      The PromptRun MUST be FAILED even if response_text is valid.
        if max_tool_calls_violation is not None:
            raise ProviderContractViolationError(
                f"OpenAI exceeded requested max_tool_calls: "
                f"requested={configured_max_tool_calls} "
                f"observed={web_tool_call_count} "
                f"(search={search_action_count}, open_page={open_page_action_count}, "
                f"find_in_page={find_in_page_action_count}, "
                f"unknown={unknown_web_action_count}).",
                provider=LLMProvider.OPENAI.value,
                evidence=_build_evidence(),
            )

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
            web_tool_call_count=usage.web_tool_call_count,
            search_action_count=usage.search_action_count,
            open_page_action_count=usage.open_page_action_count,
            find_in_page_action_count=usage.find_in_page_action_count,
            unknown_web_action_count=usage.unknown_web_action_count,
        )

        return result


def _safe_int(value: Any) -> int | None:
    """Safely convert a value to int, returning None if not an int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
