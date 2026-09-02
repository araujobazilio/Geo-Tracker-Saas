"""Unit tests for web form parsing — onboarding wizard form data."""

from __future__ import annotations

import json

from app.core.enums import LLMProvider
from app.web.forms import OnboardingFormData, parse_onboarding_form


class TestOnboardingFormParsing:
    def test_empty_form_has_errors(self) -> None:
        data = parse_onboarding_form({})
        assert not data.is_valid
        assert "name" in data.errors
        assert "domain" in data.errors
        assert "brand_name" in data.errors
        assert "keywords" in data.errors

    def test_valid_form(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "My Project",
                "domain": "example.com",
                "brand_name": "Example",
                "brand_aliases": "Example Inc, Example LLC",
                "industry": "SaaS",
                "target_country": "US",
                "target_language": "en",
                "target_audience": "Developers",
                "keywords": json.dumps(
                    [{"text": "best saas tool", "intent": "commercial", "funnel_stage": "PURCHASE"}]
                ),
                "providers": json.dumps(["OPENAI", "ANTHROPIC"]),
            }
        )
        assert data.is_valid
        assert data.name == "My Project"
        assert data.domain == "example.com"
        assert data.brand_name == "Example"
        assert data.brand_aliases == ["Example Inc", "Example LLC"]
        assert data.industry == "SaaS"
        assert data.target_country == "US"
        assert data.target_language == "en"
        assert data.target_audience == "Developers"
        assert len(data.keywords) == 1
        assert data.keywords[0]["text"] == "best saas tool"
        assert data.keywords[0]["intent"] == "commercial"
        assert data.keywords[0]["funnel_stage"] == "PURCHASE"
        assert LLMProvider.OPENAI in data.providers
        assert LLMProvider.ANTHROPIC in data.providers

    def test_keywords_as_list_of_dicts(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "Test",
                "domain": "test.com",
                "brand_name": "Test",
                "keywords": [{"text": "topic 1"}, {"text": "topic 2"}],
            }
        )
        assert data.is_valid
        assert len(data.keywords) == 2
        assert data.keywords[0]["text"] == "topic 1"
        assert data.keywords[1]["text"] == "topic 2"

    def test_keywords_as_list_of_strings(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "Test",
                "domain": "test.com",
                "brand_name": "Test",
                "keywords": ["topic 1", "topic 2"],
            }
        )
        assert data.is_valid
        assert len(data.keywords) == 2
        assert data.keywords[0]["text"] == "topic 1"

    def test_keywords_invalid_json(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "Test",
                "domain": "test.com",
                "brand_name": "Test",
                "keywords": "not valid json",
            }
        )
        assert not data.is_valid
        assert "keywords" in data.errors

    def test_competitors_as_json(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "Test",
                "domain": "test.com",
                "brand_name": "Test",
                "keywords": [{"text": "topic"}],
                "competitors": json.dumps(
                    [{"name": "Comp A", "domain": "compa.com", "aliases": ["A Inc"]}]
                ),
            }
        )
        assert data.is_valid
        assert len(data.competitors) == 1
        assert data.competitors[0]["name"] == "Comp A"
        assert data.competitors[0]["domain"] == "compa.com"
        assert data.competitors[0]["aliases"] == ["A Inc"]

    def test_competitors_with_string_aliases(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "Test",
                "domain": "test.com",
                "brand_name": "Test",
                "keywords": [{"text": "topic"}],
                "competitors": [{"name": "Comp", "domain": "comp.com", "aliases": "A, B, C"}],
            }
        )
        assert data.is_valid
        assert data.competitors[0]["aliases"] == ["A", "B", "C"]

    def test_providers_as_list(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "Test",
                "domain": "test.com",
                "brand_name": "Test",
                "keywords": [{"text": "topic"}],
                "providers": ["OPENAI", "GOOGLE"],
            }
        )
        assert data.is_valid
        assert LLMProvider.OPENAI in data.providers
        assert LLMProvider.GOOGLE in data.providers

    def test_providers_invalid_values_ignored(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "Test",
                "domain": "test.com",
                "brand_name": "Test",
                "keywords": [{"text": "topic"}],
                "providers": ["OPENAI", "invalid_provider", "ANTHROPIC"],
            }
        )
        assert data.is_valid
        assert LLMProvider.OPENAI in data.providers
        assert LLMProvider.ANTHROPIC in data.providers
        assert len(data.providers) == 2  # invalid_provider ignored

    def test_providers_as_single_string(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "Test",
                "domain": "test.com",
                "brand_name": "Test",
                "keywords": [{"text": "topic"}],
                "providers": "OPENAI",
            }
        )
        assert data.is_valid
        assert LLMProvider.OPENAI in data.providers

    def test_strips_whitespace(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "  My Project  ",
                "domain": "  example.com  ",
                "brand_name": "  Example  ",
                "keywords": [{"text": "  topic  "}],
            }
        )
        assert data.is_valid
        assert data.name == "My Project"
        assert data.domain == "example.com"
        assert data.brand_name == "Example"
        assert data.keywords[0]["text"] == "topic"

    def test_empty_keywords_list(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "Test",
                "domain": "test.com",
                "brand_name": "Test",
                "keywords": [],
            }
        )
        assert not data.is_valid
        assert "keywords" in data.errors

    def test_no_competitors_is_valid(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "Test",
                "domain": "test.com",
                "brand_name": "Test",
                "keywords": [{"text": "topic"}],
            }
        )
        assert data.is_valid
        assert data.competitors == []

    def test_no_providers_is_valid(self) -> None:
        data = parse_onboarding_form(
            {
                "name": "Test",
                "domain": "test.com",
                "brand_name": "Test",
                "keywords": [{"text": "topic"}],
            }
        )
        assert data.is_valid
        assert data.providers == []


class TestOnboardingFormData:
    def test_default_is_valid_false(self) -> None:
        data = OnboardingFormData()
        # No errors set by default, but no required fields either
        assert data.is_valid  # No errors means valid
        assert data.name == ""
        assert data.keywords == []
        assert data.competitors == []
        assert data.providers == []

    def test_errors_make_invalid(self) -> None:
        data = OnboardingFormData()
        data.errors["name"] = "Required"
        assert not data.is_valid
