"""Atomic provider evidence, source, cost, UsageEvent, and quota recording."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.enums import LLMProvider, PromptRunStatus, ProviderExecutionMode, ProviderSurface
from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.models.scan import PromptRun, ResponseSource
from app.providers.base import ProviderResult
from app.providers.errors import ProviderResponseError
from app.repositories.scan_repository import (
    PromptRunRepository,
    ResponseSourceRepository,
    ScanRepository,
)
from app.services.pricing_service import PricingService, ProviderCostCalculator
from app.services.quota_service import QuotaService

logger = get_logger("app.prompt_run_recorder")


class PromptRunResultRecorder:
    """Persist one already-obtained ProviderResult without another provider call."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._runs = PromptRunRepository(session)
        self._scans = ScanRepository(session)
        self._sources = ResponseSourceRepository(session)
        self._pricing = PricingService(session)
        self._calculator = ProviderCostCalculator()
        self._quota = QuotaService(session)

    def record(self, prompt_run_id: uuid.UUID, result: ProviderResult) -> None:
        try:
            run = self._runs.get_for_update(prompt_run_id)
            if run is None:
                raise ConflictError("PromptRun not found.")
            if run.status == PromptRunStatus.SUCCEEDED:
                self._session.commit()
                return
            if run.status != PromptRunStatus.RUNNING:
                raise ConflictError(f"Cannot record result for PromptRun in {run.status} state.")
            self._validate_contract(run, result)

            scan = self._scans.get_by_id(run.scan_id)
            if scan is None or scan.quota_reservation_id is None:
                raise ConflictError("PromptRun Scan has no active quota reservation.")
            execution_time = run.started_at or datetime.now(UTC)
            provider = LLMProvider(run.provider)
            surface = ProviderSurface(run.provider_surface)
            rule = self._pricing.resolve_optional(
                provider,
                surface,
                run.requested_model,
                execution_time,
            )
            cost = self._calculator.calculate(result, rule)
            if not cost.complete:
                logger.warning(
                    "provider_cost_unknown",
                    prompt_run_id=str(run.id),
                    provider=provider.value,
                    model=run.requested_model,
                )

            usage = result.usage
            event = self._quota.commit_ai_checks(
                reservation_id=scan.quota_reservation_id,
                quantity=1,
                usage_idempotency_key=f"prompt-run:{run.id}:usage",
                provider=provider.value,
                model=run.requested_model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_write_input_tokens=usage.cache_write_input_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                citation_tokens=usage.citation_tokens,
                search_requests=usage.search_requests,
                cost_usd=cost.cost_usd,
                provider_reported_cost_usd=cost.provider_reported_cost_usd,
                cost_source=cost.source,
                pricing_rule_id=cost.pricing_rule_id,
                prompt_run_id=run.id,
                commit_transaction=False,
            )

            run.returned_model = result.returned_model
            run.response_text = result.response_text
            run.provider_request_id = result.provider_request_id
            run.provider_response_id = result.provider_response_id
            run.finish_reason = result.finish_reason
            run.latency_ms = result.latency_ms
            run.search_used = result.search_used
            run.input_tokens = usage.input_tokens
            run.output_tokens = usage.output_tokens
            run.total_tokens = usage.total_tokens
            run.cached_input_tokens = usage.cached_input_tokens
            run.cache_write_input_tokens = usage.cache_write_input_tokens
            run.reasoning_tokens = usage.reasoning_tokens
            run.citation_tokens = usage.citation_tokens
            run.search_requests = usage.search_requests
            run.provider_reported_cost_usd = cost.provider_reported_cost_usd
            run.calculated_cost_usd = cost.calculated_cost_usd
            run.cost_usd = cost.cost_usd
            run.cost_source = cost.source
            run.pricing_rule_id = cost.pricing_rule_id
            run.usage_event_id = event.id
            run.status = PromptRunStatus.SUCCEEDED
            run.completed_at = datetime.now(UTC)
            self._sources.create_batch(
                [
                    ResponseSource(
                        prompt_run_id=run.id,
                        ordinal=ordinal,
                        url=citation.url,
                        title=citation.title,
                        source_type=citation.source_type,
                        start_index=citation.start_index,
                        end_index=citation.end_index,
                        cited_text=citation.cited_text,
                    )
                    for ordinal, citation in enumerate(result.citations, start=1)
                ]
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    @staticmethod
    def _validate_contract(run: PromptRun, result: ProviderResult) -> None:
        if (
            result.provider != LLMProvider(run.provider)
            or result.surface != ProviderSurface(run.provider_surface)
            or result.execution_mode != ProviderExecutionMode(run.execution_mode)
            or result.requested_model != run.requested_model
        ):
            raise ProviderResponseError(
                "Provider result does not match the PromptRun snapshot.",
                provider=result.provider.value,
            )
