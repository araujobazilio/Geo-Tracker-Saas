"""Regression test: ProviderCostCalculator with the pinned OpenAI pricing rule.

Proves the seeded rule works with the existing calculator using a synthetic
OpenAI ProviderResult containing representative usage:
- normal input tokens
- cached input tokens
- cache-write input tokens
- output tokens
- reasoning tokens
- at least one WEB_GROUNDED search request

Asserts:
- cost calculation is complete
- source = PRICE_RULE when no provider-reported monetary cost exists
- pricing_rule_id is preserved
- no UNKNOWN cost due to a missing rate
- reasoning is not double billed
- cached input is not double billed
- web-search cost is included exactly once per reported search request

Uses Decimal expectations.  No database or provider calls are made.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from scripts.seed_provider_pricing import _OPENAI_GPT56_TERRA

from app.core.enums import (
    CostSource,
    LLMProvider,
    ProviderExecutionMode,
    ProviderSurface,
)
from app.models.pricing import ProviderPriceRule
from app.providers.base import ProviderResult, ProviderUsage
from app.services.pricing_service import ProviderCostCalculator

_MILLION = Decimal("1000000")
_THOUSAND = Decimal("1000")
_RULE_ID = uuid.UUID("aabbccdd-1234-5678-9abc-deadbeef0001")


def _make_pinned_rule() -> ProviderPriceRule:
    """Build an unpersisted ORM rule from the pinned pricing evidence."""
    return ProviderPriceRule(
        id=_RULE_ID,
        pricing_key=_OPENAI_GPT56_TERRA.pricing_key,
        provider=_OPENAI_GPT56_TERRA.provider,
        provider_surface=_OPENAI_GPT56_TERRA.provider_surface,
        model=_OPENAI_GPT56_TERRA.model,
        effective_from=_OPENAI_GPT56_TERRA.effective_from,
        effective_to=_OPENAI_GPT56_TERRA.effective_to,
        input_per_million_usd=_OPENAI_GPT56_TERRA.input_per_million_usd,
        cached_input_per_million_usd=_OPENAI_GPT56_TERRA.cached_input_per_million_usd,
        cache_write_per_million_usd=_OPENAI_GPT56_TERRA.cache_write_per_million_usd,
        output_per_million_usd=_OPENAI_GPT56_TERRA.output_per_million_usd,
        reasoning_per_million_usd=_OPENAI_GPT56_TERRA.reasoning_per_million_usd,
        citation_per_million_usd=_OPENAI_GPT56_TERRA.citation_per_million_usd,
        search_per_1000_usd=_OPENAI_GPT56_TERRA.search_per_1000_usd,
        request_fee_usd=_OPENAI_GPT56_TERRA.request_fee_usd,
        input_tokens_include_cached=_OPENAI_GPT56_TERRA.input_tokens_include_cached,
        output_tokens_include_reasoning=_OPENAI_GPT56_TERRA.output_tokens_include_reasoning,
        verified_at=_OPENAI_GPT56_TERRA.verified_at,
        source_url=_OPENAI_GPT56_TERRA.source_url,
        notes=_OPENAI_GPT56_TERRA.notes,
    )


def _make_result(
    usage: ProviderUsage,
    *,
    search_used: bool = True,
    provider_reported_cost_usd: Decimal | None = None,
) -> ProviderResult:
    """Build a synthetic OpenAI ProviderResult."""
    return ProviderResult(
        provider=LLMProvider.OPENAI,
        surface=ProviderSurface.OPENAI_RESPONSES_API,
        execution_mode=ProviderExecutionMode.WEB_GROUNDED,
        requested_model="gpt-5.6-terra",
        returned_model="gpt-5.6-terra-2026-07-30",
        response_text="Synthetic grounded response",
        citations=(),
        usage=usage,
        provider_request_id="req-123",
        provider_response_id="resp-456",
        finish_reason="stop",
        latency_ms=1500,
        search_used=search_used,
        provider_reported_cost_usd=provider_reported_cost_usd,
        metadata={},
    )


class TestPinnedRuleCalculatorRegression:
    """Prove the pinned OpenAI pricing rule produces correct costs."""

    def test_complete_cost_calculation(self) -> None:
        """Full usage with all components produces a complete cost."""
        rule = _make_pinned_rule()
        usage = ProviderUsage(
            input_tokens=10000,
            output_tokens=5000,
            total_tokens=15000,
            cached_input_tokens=3000,
            cache_write_input_tokens=2000,
            reasoning_tokens=2000,
            citation_tokens=0,
            search_requests=3,
        )
        result = _make_result(usage, search_used=True)
        calc = ProviderCostCalculator()
        comp = calc.calculate(result, rule)

        assert comp.complete is True, "Cost calculation must be complete"
        assert comp.source == CostSource.PRICE_RULE, "Source must be PRICE_RULE"
        assert comp.pricing_rule_id == _RULE_ID, "pricing_rule_id must be preserved"
        assert comp.cost_usd is not None, "cost_usd must not be None"
        assert comp.cost_usd > Decimal("0"), "Cost must be positive"

    def test_reasoning_not_double_billed(self) -> None:
        """Reasoning tokens must not be double-billed with output tokens.

        With output_tokens_include_reasoning=True:
        - reasoning_tokens are billed at reasoning_per_million_usd
        - output_tokens - reasoning_tokens are billed at output_per_million_usd
        - Total output cost = reasoning_cost + (output - reasoning) * output_rate
        """
        rule = _make_pinned_rule()
        # All output tokens are reasoning.
        usage_all_reasoning = ProviderUsage(
            input_tokens=1000,
            output_tokens=4000,
            total_tokens=5000,
            cached_input_tokens=0,
            cache_write_input_tokens=0,
            reasoning_tokens=4000,
            citation_tokens=0,
            search_requests=0,
        )
        result = _make_result(usage_all_reasoning, search_used=False)
        calc = ProviderCostCalculator()
        comp = calc.calculate(result, rule)

        assert comp.complete is True
        # Output cost = 4000 reasoning * 12.00/M = 0.048
        # Input cost = 1000 * 2.00/M = 0.002
        # Total = 0.050
        expected = (
            Decimal("1000") * Decimal("2.00") / _MILLION
            + Decimal("4000") * Decimal("12.00") / _MILLION
        )
        assert comp.cost_usd == expected, f"Expected {expected}, got {comp.cost_usd}"

    def test_cached_input_not_double_billed(self) -> None:
        """Cached input tokens must not be double-billed with uncached input.

        With input_tokens_include_cached=True:
        - cached tokens are billed at cached_input_per_million_usd
        - (input_tokens - cached_tokens) are billed at input_per_million_usd
        """
        rule = _make_pinned_rule()
        usage = ProviderUsage(
            input_tokens=10000,
            output_tokens=1000,
            total_tokens=11000,
            cached_input_tokens=4000,
            cache_write_input_tokens=0,
            reasoning_tokens=0,
            citation_tokens=0,
            search_requests=0,
        )
        result = _make_result(usage, search_used=False)
        calc = ProviderCostCalculator()
        comp = calc.calculate(result, rule)

        assert comp.complete is True
        # Uncached input = 10000 - 4000 = 6000 * 2.00/M = 0.012
        # Cached input = 4000 * 0.20/M = 0.0008
        # Output = 1000 * 12.00/M = 0.012
        expected = (
            Decimal("6000") * Decimal("2.00") / _MILLION
            + Decimal("4000") * Decimal("0.20") / _MILLION
            + Decimal("1000") * Decimal("12.00") / _MILLION
        )
        assert comp.cost_usd == expected, f"Expected {expected}, got {comp.cost_usd}"

    def test_cache_write_cost_included(self) -> None:
        """Cache-write input tokens must be billed at cache_write_per_million_usd."""
        rule = _make_pinned_rule()
        usage = ProviderUsage(
            input_tokens=1000,
            output_tokens=1000,
            total_tokens=2000,
            cached_input_tokens=0,
            cache_write_input_tokens=5000,
            reasoning_tokens=0,
            citation_tokens=0,
            search_requests=0,
        )
        result = _make_result(usage, search_used=False)
        calc = ProviderCostCalculator()
        comp = calc.calculate(result, rule)

        assert comp.complete is True
        # Input = 1000 * 2.00/M = 0.002
        # Cache write = 5000 * 2.50/M = 0.0125
        # Output = 1000 * 12.00/M = 0.012
        expected = (
            Decimal("1000") * Decimal("2.00") / _MILLION
            + Decimal("5000") * Decimal("2.50") / _MILLION
            + Decimal("1000") * Decimal("12.00") / _MILLION
        )
        assert comp.cost_usd == expected, f"Expected {expected}, got {comp.cost_usd}"

    def test_web_search_cost_included_once(self) -> None:
        """Web-search cost must be included exactly once per search request."""
        rule = _make_pinned_rule()
        usage = ProviderUsage(
            input_tokens=1000,
            output_tokens=1000,
            total_tokens=2000,
            cached_input_tokens=0,
            cache_write_input_tokens=0,
            reasoning_tokens=0,
            citation_tokens=0,
            search_requests=5,
        )
        result = _make_result(usage, search_used=True)
        calc = ProviderCostCalculator()
        comp = calc.calculate(result, rule)

        assert comp.complete is True
        # Input = 1000 * 2.00/M = 0.002
        # Output = 1000 * 12.00/M = 0.012
        # Search = 5 * 10.00/1000 = 0.050
        expected = (
            Decimal("1000") * Decimal("2.00") / _MILLION
            + Decimal("1000") * Decimal("12.00") / _MILLION
            + Decimal("5") * Decimal("10.00") / _THOUSAND
        )
        assert comp.cost_usd == expected, f"Expected {expected}, got {comp.cost_usd}"

    def test_no_unknown_cost_with_seeded_rule(self) -> None:
        """With the seeded rule, cost must never be UNKNOWN for valid usage."""
        rule = _make_pinned_rule()
        usage = ProviderUsage(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cached_input_tokens=20,
            cache_write_input_tokens=10,
            reasoning_tokens=30,
            citation_tokens=0,
            search_requests=1,
        )
        result = _make_result(usage, search_used=True)
        calc = ProviderCostCalculator()
        comp = calc.calculate(result, rule)

        assert comp.source != CostSource.UNKNOWN, "Must not be UNKNOWN"
        assert comp.complete is True

    def test_full_representative_usage(self) -> None:
        """Representative usage with all components produces exact expected cost."""
        rule = _make_pinned_rule()
        usage = ProviderUsage(
            input_tokens=50000,
            output_tokens=8000,
            total_tokens=58000,
            cached_input_tokens=15000,
            cache_write_input_tokens=10000,
            reasoning_tokens=4000,
            citation_tokens=0,
            search_requests=10,
        )
        result = _make_result(usage, search_used=True)
        calc = ProviderCostCalculator()
        comp = calc.calculate(result, rule)

        assert comp.complete is True
        assert comp.source == CostSource.PRICE_RULE
        assert comp.pricing_rule_id == _RULE_ID

        # Uncached input = (50000 - 15000) * 2.00/M = 35000 * 2.00/M = 0.070
        # Cached input = 15000 * 0.20/M = 0.003
        # Cache write = 10000 * 2.50/M = 0.025
        # Reasoning = 4000 * 12.00/M = 0.048
        # Non-reasoning output = (8000 - 4000) * 12.00/M = 4000 * 12.00/M = 0.048
        # Search = 10 * 10.00/1000 = 0.100
        expected = (
            Decimal("35000") * Decimal("2.00") / _MILLION
            + Decimal("15000") * Decimal("0.20") / _MILLION
            + Decimal("10000") * Decimal("2.50") / _MILLION
            + Decimal("4000") * Decimal("12.00") / _MILLION
            + Decimal("4000") * Decimal("12.00") / _MILLION
            + Decimal("10") * Decimal("10.00") / _THOUSAND
        )
        assert comp.cost_usd == expected, f"Expected {expected}, got {comp.cost_usd}"
