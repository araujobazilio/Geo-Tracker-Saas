"""Unit tests for the confidence level classifier (Phase 8).

Tests the deterministic v1 heuristic thresholds for
INSUFFICIENT, LOW, MEDIUM, HIGH.

This is a PRODUCT EVIDENCE-QUALITY LABEL, NOT statistical confidence.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.confidence_metrics_service import (
    CONFIDENCE_METHODOLOGY_VERSION,
    MeasurementConfidenceLevel,
    classify_confidence_level,
)


def _D(value: str) -> Decimal:
    return Decimal(value)


# --- INSUFFICIENT tests ---


def test_insufficient_when_fewer_than_2_valid_rounds() -> None:
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=1,
        measurement_coverage=_D("100"),
        repeat_sufficiency=_D("100"),
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.INSUFFICIENT


def test_insufficient_when_zero_valid_rounds() -> None:
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=0,
        measurement_coverage=_D("0"),
        repeat_sufficiency=_D("0"),
        mention_stability=None,
    )
    assert level == MeasurementConfidenceLevel.INSUFFICIENT


def test_insufficient_when_coverage_below_50() -> None:
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=2,
        measurement_coverage=_D("49.9999"),
        repeat_sufficiency=_D("100"),
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.INSUFFICIENT


def test_insufficient_when_repeat_sufficiency_below_50() -> None:
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=2,
        measurement_coverage=_D("100"),
        repeat_sufficiency=_D("49.9999"),
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.INSUFFICIENT


def test_insufficient_when_coverage_is_none() -> None:
    """None coverage means 0 successful out of 0 planned -> insufficient."""
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=0,
        measurement_coverage=None,
        repeat_sufficiency=None,
        mention_stability=None,
    )
    assert level == MeasurementConfidenceLevel.INSUFFICIENT


# --- LOW tests ---


def test_low_when_coverage_below_75() -> None:
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=2,
        measurement_coverage=_D("74.9999"),
        repeat_sufficiency=_D("100"),
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.LOW


def test_low_when_repeat_sufficiency_below_75() -> None:
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=2,
        measurement_coverage=_D("100"),
        repeat_sufficiency=_D("74.9999"),
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.LOW


def test_low_when_mention_stability_below_60() -> None:
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=2,
        measurement_coverage=_D("100"),
        repeat_sufficiency=_D("100"),
        mention_stability=_D("59.9999"),
    )
    assert level == MeasurementConfidenceLevel.LOW


# --- MEDIUM tests ---


def test_medium_with_default_3_repeats_and_good_metrics() -> None:
    """Default 3 repeats can reach at most MEDIUM under v1."""
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=3,
        measurement_coverage=_D("100"),
        repeat_sufficiency=_D("100"),
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.MEDIUM


def test_medium_with_4_repeats_and_good_metrics() -> None:
    level = classify_confidence_level(
        repeat_count=4,
        valid_round_count=4,
        measurement_coverage=_D("100"),
        repeat_sufficiency=_D("100"),
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.MEDIUM


def test_medium_at_exact_low_thresholds() -> None:
    """At exactly 75% coverage, 75% repeat sufficiency, 60% mention stability -> MEDIUM."""
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=2,
        measurement_coverage=_D("75"),
        repeat_sufficiency=_D("75"),
        mention_stability=_D("60"),
    )
    assert level == MeasurementConfidenceLevel.MEDIUM


# --- HIGH tests ---


def test_high_with_5_repeats_and_all_thresholds_met() -> None:
    level = classify_confidence_level(
        repeat_count=5,
        valid_round_count=5,
        measurement_coverage=_D("90"),
        repeat_sufficiency=_D("90"),
        mention_stability=_D("80"),
    )
    assert level == MeasurementConfidenceLevel.HIGH


def test_high_with_10_repeats_and_perfect_metrics() -> None:
    level = classify_confidence_level(
        repeat_count=10,
        valid_round_count=10,
        measurement_coverage=_D("100"),
        repeat_sufficiency=_D("100"),
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.HIGH


def test_high_not_reached_with_4_repeats_even_if_perfect() -> None:
    """HIGH requires repeat_count >= 5."""
    level = classify_confidence_level(
        repeat_count=4,
        valid_round_count=4,
        measurement_coverage=_D("100"),
        repeat_sufficiency=_D("100"),
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.MEDIUM


def test_high_not_reached_with_5_repeats_but_coverage_below_90() -> None:
    level = classify_confidence_level(
        repeat_count=5,
        valid_round_count=5,
        measurement_coverage=_D("89.9999"),
        repeat_sufficiency=_D("100"),
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.MEDIUM


def test_high_not_reached_with_5_repeats_but_repeat_sufficiency_below_90() -> None:
    level = classify_confidence_level(
        repeat_count=5,
        valid_round_count=5,
        measurement_coverage=_D("100"),
        repeat_sufficiency=_D("89.9999"),
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.MEDIUM


def test_high_not_reached_with_5_repeats_but_mention_stability_below_80() -> None:
    level = classify_confidence_level(
        repeat_count=5,
        valid_round_count=5,
        measurement_coverage=_D("100"),
        repeat_sufficiency=_D("100"),
        mention_stability=_D("79.9999"),
    )
    assert level == MeasurementConfidenceLevel.MEDIUM


# --- Methodology version ---


def test_methodology_version_is_repeat_reliability_v1() -> None:
    assert CONFIDENCE_METHODOLOGY_VERSION == "repeat-reliability-v1"


def test_measurement_confidence_level_values() -> None:
    assert MeasurementConfidenceLevel.INSUFFICIENT == "INSUFFICIENT"
    assert MeasurementConfidenceLevel.LOW == "LOW"
    assert MeasurementConfidenceLevel.MEDIUM == "MEDIUM"
    assert MeasurementConfidenceLevel.HIGH == "HIGH"


# --- Edge cases ---


def test_none_mention_stability_does_not_cause_low() -> None:
    """None mention_stability (no repeat-analyzable cells) should not trigger LOW."""
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=2,
        measurement_coverage=_D("100"),
        repeat_sufficiency=_D("100"),
        mention_stability=None,
    )
    assert level == MeasurementConfidenceLevel.MEDIUM


def test_none_repeat_sufficiency_does_not_cause_low() -> None:
    """None repeat_sufficiency (no planned cells) should not trigger LOW."""
    level = classify_confidence_level(
        repeat_count=3,
        valid_round_count=2,
        measurement_coverage=_D("100"),
        repeat_sufficiency=None,
        mention_stability=_D("100"),
    )
    assert level == MeasurementConfidenceLevel.MEDIUM
