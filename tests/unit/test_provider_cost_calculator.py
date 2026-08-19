"""Unit tests for exact, provider-aware cost calculation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.core.enums import (
    CostSource,
    LLMProvider,
    ProviderExecutionMode,
    ProviderSurface,
)
from app.models.pricing import ProviderPriceRule
from app.providers.base import ProviderResult, ProviderUsage
from app.services.pricing_service import ProviderCostCalculator

_RULE_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def make_rule(**overrides: Any) -> ProviderPriceRule:
    """Build an unpersisted ORM price rule; no database session is needed."""
    values: dict[str, Any] = {
        "id": _RULE_ID,
        "pricing_key": "openai:test-model:2025-01-01",
        "provider": LLMProvider.OPENAI,
        "provider_surface": ProviderSurface.OPENAI_RESPONSES_API,
        "model": "test-model",
        "effective_from": datetime(2025, 1, 1, tzinfo=UTC),
        "effective_to": None,
        "input_per_million_usd": None,
        "cached_input_per_million_usd": None,
        "cache_write_per_million_usd": None,
        "output_per_million_usd": None,
        "reasoning_per_million_usd": None,
        "citation_per_million_usd": None,
        "search_per_1000_usd": None,
        "request_fee_usd": None,
        "input_tokens_include_cached": False,
        "output_tokens_include_reasoning": False,
        "verified_at": datetime(2025, 1, 1, tzinfo=UTC),
        "source_url": "https://example.test/pricing",
        "notes": "Synthetic unit-test rule",
    }
    values.update(overrides)
    return ProviderPriceRule(**values)


def make_usage(**overrides: int | None) -> ProviderUsage:
    """Build immutable usage with explicit zeroes for unused components."""
    values: dict[str, int | None] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "reasoning_tokens": 0,
        "citation_tokens": 0,
        "search_requests": 0,
    }
    values.update(overrides)
    return ProviderUsage(**values)


def make_result(
    usage: ProviderUsage,
    *,
    search_used: bool = False,
    provider_reported_cost_usd: Decimal | None = None,
) -> ProviderResult:
    """Build an immutable normalized provider result without I/O."""
    return ProviderResult(
        provider=LLMProvider.OPENAI,
        surface=ProviderSurface.OPENAI_RESPONSES_API,
        execution_mode=ProviderExecutionMode.MODEL_ONLY,
        requested_model="test-model",
        returned_model="test-model-2025-01-01",
        response_text="Synthetic response",
        citations=(),
        usage=usage,
        provider_request_id="request-id",
        provider_response_id="response-id",
        finish_reason="stop",
        latency_ms=1,
        search_used=search_used,
        provider_reported_cost_usd=provider_reported_cost_usd,
        metadata={},
    )


def calculate(
    usage: ProviderUsage,
    rule: ProviderPriceRule | None,
    *,
    search_used: bool = False,
    provider_reported_cost_usd: Decimal | None = None,
):
    return ProviderCostCalculator().calculate(
        make_result(
            usage,
            search_used=search_used,
            provider_reported_cost_usd=provider_reported_cost_usd,
        ),
        rule,
    )


def assert_rule_cost(computation: Any, expected: Decimal) -> None:
    assert computation.cost_usd == expected
    assert computation.calculated_cost_usd == expected
    assert computation.provider_reported_cost_usd is None
    assert computation.source is CostSource.PRICE_RULE
    assert computation.complete is True
    assert computation.pricing_rule_id == _RULE_ID


def assert_unknown(computation: Any) -> None:
    assert computation.cost_usd is None
    assert computation.calculated_cost_usd is None
    assert computation.provider_reported_cost_usd is None
    assert computation.source is CostSource.UNKNOWN
    assert computation.complete is False
    assert computation.pricing_rule_id is None


def test_basic_input_and_output_token_cost() -> None:
    rule = make_rule(
        input_per_million_usd=Decimal("2"),
        output_per_million_usd=Decimal("8"),
    )

    computation = calculate(
        make_usage(input_tokens=250_000, output_tokens=125_000),
        rule,
    )

    assert_rule_cost(computation, Decimal("1.5"))


def test_input_count_including_cached_tokens_excludes_cache_from_regular_input() -> None:
    rule = make_rule(
        input_per_million_usd=Decimal("10"),
        cached_input_per_million_usd=Decimal("2"),
        input_tokens_include_cached=True,
    )

    computation = calculate(
        make_usage(input_tokens=1_000, cached_input_tokens=400),
        rule,
    )

    # 600 ordinary tokens at $10/M plus 400 cached tokens at $2/M.
    assert_rule_cost(computation, Decimal("0.0068"))
    assert computation.cost_usd != Decimal("0.0108")  # Naive cached double charge.


def test_separately_reported_cache_is_added_without_reducing_input() -> None:
    rule = make_rule(
        input_per_million_usd=Decimal("10"),
        cached_input_per_million_usd=Decimal("2"),
        input_tokens_include_cached=False,
    )

    computation = calculate(
        make_usage(input_tokens=600, cached_input_tokens=400),
        rule,
    )

    assert_rule_cost(computation, Decimal("0.0068"))


def test_cache_write_tokens_use_cache_write_rate() -> None:
    rule = make_rule(cache_write_per_million_usd=Decimal("12.5"))

    computation = calculate(make_usage(cache_write_input_tokens=80_000), rule)

    assert_rule_cost(computation, Decimal("1.000"))


def test_search_requests_use_per_thousand_rate() -> None:
    rule = make_rule(search_per_1000_usd=Decimal("7.50"))

    computation = calculate(
        make_usage(search_requests=4),
        rule,
        search_used=True,
    )

    assert_rule_cost(computation, Decimal("0.030"))


def test_reasoning_included_in_output_is_not_double_charged() -> None:
    rule = make_rule(
        output_per_million_usd=Decimal("8"),
        reasoning_per_million_usd=Decimal("20"),
        output_tokens_include_reasoning=True,
    )

    computation = calculate(
        make_usage(output_tokens=1_000, reasoning_tokens=250),
        rule,
    )

    # 750 non-reasoning output tokens plus 250 reasoning tokens.
    assert_rule_cost(computation, Decimal("0.011"))
    assert computation.cost_usd != Decimal("0.013")  # Naive reasoning double charge.


def test_google_like_reasoning_excluded_from_output_uses_separate_fields() -> None:
    rule = make_rule(
        provider=LLMProvider.GOOGLE,
        provider_surface=ProviderSurface.GOOGLE_INTERACTIONS_API,
        output_per_million_usd=Decimal("8"),
        reasoning_per_million_usd=Decimal("20"),
        output_tokens_include_reasoning=False,
    )

    computation = calculate(
        make_usage(output_tokens=750, reasoning_tokens=250),
        rule,
    )

    assert_rule_cost(computation, Decimal("0.011"))


def test_citation_tokens_use_citation_rate() -> None:
    rule = make_rule(citation_per_million_usd=Decimal("3.25"))

    computation = calculate(make_usage(citation_tokens=40_000), rule)

    assert_rule_cost(computation, Decimal("0.13000"))


def test_request_fee_is_added_once() -> None:
    rule = make_rule(request_fee_usd=Decimal("0.00425"))

    computation = calculate(make_usage(), rule)

    assert_rule_cost(computation, Decimal("0.00425"))


def test_decimal_precision_is_exact_and_never_converted_to_float() -> None:
    rule = make_rule(
        input_per_million_usd=Decimal("0.1234567891"),
        output_per_million_usd=Decimal("9.8765432109"),
        request_fee_usd=Decimal("0.0000000001"),
    )

    computation = calculate(
        make_usage(input_tokens=1, output_tokens=3),
        rule,
    )

    expected = (
        Decimal("0.0000001234567891") + Decimal("0.0000296296296327") + Decimal("0.0000000001")
    )
    assert_rule_cost(computation, expected)
    assert computation.cost_usd == Decimal("0.0000297531864218")
    assert isinstance(computation.cost_usd, Decimal)


@pytest.mark.parametrize(
    ("usage_field", "rate_field"),
    [
        ("input_tokens", "input_per_million_usd"),
        ("cached_input_tokens", "cached_input_per_million_usd"),
        ("cache_write_input_tokens", "cache_write_per_million_usd"),
        ("output_tokens", "output_per_million_usd"),
        ("reasoning_tokens", "reasoning_per_million_usd"),
        ("citation_tokens", "citation_per_million_usd"),
    ],
)
def test_reported_usage_with_unknown_required_rate_is_unknown(
    usage_field: str,
    rate_field: str,
) -> None:
    usage = make_usage(**{usage_field: 1})
    rule = make_rule(**{rate_field: None})

    assert_unknown(calculate(usage, rule))


def test_configured_rate_with_unreported_required_usage_is_unknown() -> None:
    rule = make_rule(input_per_million_usd=Decimal("1.25"))

    computation = calculate(make_usage(input_tokens=None), rule)

    assert_unknown(computation)


def test_unknown_search_count_with_search_used_and_search_rate_is_unknown() -> None:
    rule = make_rule(search_per_1000_usd=Decimal("5"))

    computation = calculate(
        make_usage(search_requests=None),
        rule,
        search_used=True,
    )

    assert_unknown(computation)


def test_provider_reported_cost_wins_while_calculated_cost_is_preserved() -> None:
    rule = make_rule(
        input_per_million_usd=Decimal("2"),
        output_per_million_usd=Decimal("8"),
    )

    computation = calculate(
        make_usage(input_tokens=250_000, output_tokens=125_000),
        rule,
        provider_reported_cost_usd=Decimal("1.23456789"),
    )

    assert computation.cost_usd == Decimal("1.23456789")
    assert computation.calculated_cost_usd == Decimal("1.5")
    assert computation.provider_reported_cost_usd == Decimal("1.23456789")
    assert computation.source is CostSource.PROVIDER_REPORTED
    assert computation.complete is True
    assert computation.pricing_rule_id == _RULE_ID


@pytest.mark.parametrize(
    "invalid_provider_cost",
    [
        Decimal("-0.01"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_negative_and_nonfinite_provider_costs_are_ignored(
    invalid_provider_cost: Decimal,
) -> None:
    rule = make_rule(input_per_million_usd=Decimal("2"))

    computation = calculate(
        make_usage(input_tokens=500_000),
        rule,
        provider_reported_cost_usd=invalid_provider_cost,
    )

    assert_rule_cost(computation, Decimal("1"))


def test_missing_price_rule_is_unknown_without_database_access() -> None:
    computation = calculate(make_usage(input_tokens=1, output_tokens=1), None)

    assert_unknown(computation)


def test_cached_tokens_greater_than_inclusive_input_count_are_unknown() -> None:
    rule = make_rule(
        input_per_million_usd=Decimal("10"),
        cached_input_per_million_usd=Decimal("2"),
        input_tokens_include_cached=True,
    )

    computation = calculate(
        make_usage(input_tokens=399, cached_input_tokens=400),
        rule,
    )

    assert_unknown(computation)


def test_reasoning_tokens_greater_than_inclusive_output_count_are_unknown() -> None:
    rule = make_rule(
        output_per_million_usd=Decimal("8"),
        reasoning_per_million_usd=Decimal("20"),
        output_tokens_include_reasoning=True,
    )

    computation = calculate(
        make_usage(output_tokens=249, reasoning_tokens=250),
        rule,
    )

    assert_unknown(computation)
