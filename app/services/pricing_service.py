"""Exact versioned price resolution and Decimal provider cost calculation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.enums import CostSource, LLMProvider, ProviderSurface
from app.core.exceptions import PricingConfigurationError, PricingRuleNotFoundError
from app.models.pricing import ProviderPriceRule
from app.providers.base import ProviderResult

_MILLION = Decimal("1000000")
_THOUSAND = Decimal("1000")


@dataclass(frozen=True)
class CostComputation:
    cost_usd: Decimal | None
    calculated_cost_usd: Decimal | None
    provider_reported_cost_usd: Decimal | None
    source: CostSource
    complete: bool
    pricing_rule_id: uuid.UUID | None


class PricingService:
    """Resolve one exact-model rule effective at execution time."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def resolve(
        self,
        provider: LLMProvider,
        surface: ProviderSurface,
        requested_model: str,
        execution_time: datetime,
    ) -> ProviderPriceRule:
        rules = list(
            self._session.execute(
                select(ProviderPriceRule).where(
                    ProviderPriceRule.provider == provider,
                    ProviderPriceRule.provider_surface == surface,
                    ProviderPriceRule.model == requested_model,
                    ProviderPriceRule.effective_from <= execution_time,
                    or_(
                        ProviderPriceRule.effective_to.is_(None),
                        execution_time < ProviderPriceRule.effective_to,
                    ),
                )
            ).scalars()
        )
        if not rules:
            raise PricingRuleNotFoundError(
                f"No verified price rule for {provider.value}/{surface.value}/{requested_model}."
            )
        if len(rules) != 1:
            raise PricingConfigurationError(
                f"Multiple price rules apply to {provider.value}/{surface.value}/{requested_model}."
            )
        return rules[0]

    def resolve_optional(
        self,
        provider: LLMProvider,
        surface: ProviderSurface,
        requested_model: str,
        execution_time: datetime,
    ) -> ProviderPriceRule | None:
        try:
            return self.resolve(provider, surface, requested_model, execution_time)
        except PricingRuleNotFoundError:
            return None


class ProviderCostCalculator:
    """Compute provider cost without floats or fuzzy pricing assumptions."""

    def calculate(
        self,
        result: ProviderResult,
        rule: ProviderPriceRule | None,
    ) -> CostComputation:
        provider_cost = self._valid_money(result.provider_reported_cost_usd)
        calculated_cost, local_complete = self._calculate_local(result, rule)

        if provider_cost is not None:
            return CostComputation(
                cost_usd=provider_cost,
                calculated_cost_usd=calculated_cost,
                provider_reported_cost_usd=provider_cost,
                source=CostSource.PROVIDER_REPORTED,
                complete=True,
                pricing_rule_id=rule.id if rule is not None and local_complete else None,
            )
        if local_complete and calculated_cost is not None and rule is not None:
            return CostComputation(
                cost_usd=calculated_cost,
                calculated_cost_usd=calculated_cost,
                provider_reported_cost_usd=None,
                source=CostSource.PRICE_RULE,
                complete=True,
                pricing_rule_id=rule.id,
            )
        return CostComputation(
            cost_usd=None,
            calculated_cost_usd=None,
            provider_reported_cost_usd=None,
            source=CostSource.UNKNOWN,
            complete=False,
            pricing_rule_id=None,
        )

    def _calculate_local(
        self,
        result: ProviderResult,
        rule: ProviderPriceRule | None,
    ) -> tuple[Decimal | None, bool]:
        if rule is None:
            return None, False

        usage = result.usage
        total = Decimal("0")

        input_tokens = usage.input_tokens
        cached_tokens = usage.cached_input_tokens
        if not self._known_for_rate(input_tokens, rule.input_per_million_usd):
            return None, False
        if not self._known_for_rate(cached_tokens, rule.cached_input_per_million_usd):
            return None, False

        billable_input = input_tokens or 0
        if rule.input_tokens_include_cached:
            cached = cached_tokens or 0
            if cached > billable_input:
                return None, False
            billable_input -= cached
        total += self._token_cost(billable_input, rule.input_per_million_usd)
        total += self._token_cost(cached_tokens or 0, rule.cached_input_per_million_usd)

        if not self._known_for_rate(
            usage.cache_write_input_tokens, rule.cache_write_per_million_usd
        ):
            return None, False
        total += self._token_cost(
            usage.cache_write_input_tokens or 0, rule.cache_write_per_million_usd
        )

        output_tokens = usage.output_tokens
        reasoning_tokens = usage.reasoning_tokens
        if not self._known_for_rate(output_tokens, rule.output_per_million_usd):
            return None, False
        if not self._known_for_rate(reasoning_tokens, rule.reasoning_per_million_usd):
            return None, False

        billable_output = output_tokens or 0
        reasoning = reasoning_tokens or 0
        if rule.output_tokens_include_reasoning:
            if reasoning > billable_output:
                return None, False
            if rule.reasoning_per_million_usd is not None:
                billable_output -= reasoning
                total += self._token_cost(reasoning, rule.reasoning_per_million_usd)
        elif reasoning:
            if rule.reasoning_per_million_usd is None:
                return None, False
            total += self._token_cost(reasoning, rule.reasoning_per_million_usd)
        total += self._token_cost(billable_output, rule.output_per_million_usd)

        if not self._known_for_rate(usage.citation_tokens, rule.citation_per_million_usd):
            return None, False
        total += self._token_cost(usage.citation_tokens or 0, rule.citation_per_million_usd)

        if rule.search_per_1000_usd is not None:
            if usage.search_requests is None and result.search_used:
                return None, False
            total += Decimal(usage.search_requests or 0) * rule.search_per_1000_usd / _THOUSAND

        if rule.request_fee_usd is not None:
            total += rule.request_fee_usd
        return total, True

    @staticmethod
    def _known_for_rate(tokens: int | None, rate: Decimal | None) -> bool:
        if rate is None:
            return tokens in (None, 0)
        return tokens is not None

    @staticmethod
    def _token_cost(tokens: int, rate: Decimal | None) -> Decimal:
        if rate is None or tokens == 0:
            return Decimal("0")
        return Decimal(tokens) * rate / _MILLION

    @staticmethod
    def _valid_money(value: Decimal | None) -> Decimal | None:
        if value is None or not value.is_finite() or value < 0:
            return None
        return value
