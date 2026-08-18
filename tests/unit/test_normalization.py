"""Unit tests for normalization utilities."""

from __future__ import annotations

import pytest

from app.core.exceptions import ValidationError
from app.core.normalization import (
    normalize_brand_aliases,
    normalize_country,
    normalize_domain,
    normalize_keyword,
    normalize_language,
    normalize_text_for_comparison,
)


class TestNormalizeDomain:
    def test_simple_domain(self) -> None:
        assert normalize_domain("example.com") == "example.com"

    def test_strip_scheme_http(self) -> None:
        assert normalize_domain("http://example.com") == "example.com"

    def test_strip_scheme_https(self) -> None:
        assert normalize_domain("https://example.com") == "example.com"

    def test_strip_www(self) -> None:
        assert normalize_domain("www.example.com") == "example.com"

    def test_strip_www_with_scheme(self) -> None:
        assert normalize_domain("https://www.example.com") == "example.com"

    def test_strip_path(self) -> None:
        assert normalize_domain("https://example.com/some/page") == "example.com"

    def test_strip_query(self) -> None:
        assert normalize_domain("https://example.com?foo=bar") == "example.com"

    def test_strip_fragment(self) -> None:
        assert normalize_domain("https://example.com#section") == "example.com"

    def test_strip_trailing_dot(self) -> None:
        assert normalize_domain("example.com.") == "example.com"

    def test_strip_port(self) -> None:
        assert normalize_domain("example.com:8080") == "example.com"

    def test_lowercase(self) -> None:
        assert normalize_domain("Example.COM") == "example.com"

    def test_strip_userinfo(self) -> None:
        assert normalize_domain("user:pass@example.com") == "example.com"

    def test_empty_raises(self) -> None:
        with pytest.raises(ValidationError):
            normalize_domain("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValidationError):
            normalize_domain("   ")

    def test_invalid_hostname_raises(self) -> None:
        with pytest.raises(ValidationError):
            normalize_domain("-invalid.com")

    def test_normalization_dedup(self) -> None:
        """All variants of the same domain normalize to the same value."""
        variants = [
            "example.com",
            "www.example.com",
            "https://example.com/",
            "https://www.example.com/some/page",
            "HTTP://EXAMPLE.COM",
            "example.com.",
        ]
        results = {normalize_domain(v) for v in variants}
        assert results == {"example.com"}


class TestNormalizeKeyword:
    def test_simple_keyword(self) -> None:
        display, normalized = normalize_keyword("best crm")
        assert display == "best crm"
        assert normalized == "best crm"

    def test_trim_whitespace(self) -> None:
        display, normalized = normalize_keyword("  best crm  ")
        assert display == "best crm"
        assert normalized == "best crm"

    def test_collapse_internal_whitespace(self) -> None:
        display, normalized = normalize_keyword("best   crm")
        assert display == "best crm"
        assert normalized == "best crm"

    def test_lowercase_normalized(self) -> None:
        _, normalized = normalize_keyword("Best CRM")
        assert normalized == "best crm"

    def test_preserves_display_form(self) -> None:
        display, _ = normalize_keyword("Best CRM")
        assert display == "Best CRM"

    def test_empty_raises(self) -> None:
        with pytest.raises(ValidationError):
            normalize_keyword("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValidationError):
            normalize_keyword("   ")


class TestNormalizeCountry:
    def test_uppercase(self) -> None:
        assert normalize_country("us") == "US"

    def test_already_uppercase(self) -> None:
        assert normalize_country("US") == "US"

    def test_none_returns_none(self) -> None:
        assert normalize_country(None) is None

    def test_empty_returns_none(self) -> None:
        assert normalize_country("") is None

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValidationError):
            normalize_country("USA")

    def test_invalid_single_char_raises(self) -> None:
        with pytest.raises(ValidationError):
            normalize_country("U")


class TestNormalizeLanguage:
    def test_simple_en(self) -> None:
        assert normalize_language("en") == "en"

    def test_en_us(self) -> None:
        assert normalize_language("en-US") == "en-us"

    def test_lowercase(self) -> None:
        assert normalize_language("EN") == "en"

    def test_pt_br(self) -> None:
        assert normalize_language("pt-BR") == "pt-br"

    def test_none_returns_none(self) -> None:
        assert normalize_language(None) is None

    def test_empty_returns_none(self) -> None:
        assert normalize_language("") is None

    def test_unsupported_raises(self) -> None:
        with pytest.raises(ValidationError):
            normalize_language("fr")

    def test_unsupported_raises_with_message(self) -> None:
        with pytest.raises(ValidationError, match="not supported"):
            normalize_language("es")


class TestNormalizeBrandAliases:
    def test_trim_whitespace(self) -> None:
        assert normalize_brand_aliases(["  Acme  ", " Beta "]) == ["Acme", "Beta"]

    def test_remove_empty(self) -> None:
        assert normalize_brand_aliases(["Acme", "", "  ", "Beta"]) == ["Acme", "Beta"]

    def test_deduplicate_case_insensitive(self) -> None:
        result = normalize_brand_aliases(["Acme", "acme", "ACME"])
        assert result == ["Acme"]

    def test_none_returns_empty(self) -> None:
        assert normalize_brand_aliases(None) == []

    def test_empty_returns_empty(self) -> None:
        assert normalize_brand_aliases([]) == []

    def test_preserves_first_display_form(self) -> None:
        result = normalize_brand_aliases(["Acme Inc", "acme inc"])
        assert result == ["Acme Inc"]


class TestNormalizeTextForComparison:
    def test_lowercase(self) -> None:
        assert normalize_text_for_comparison("Hello") == "hello"

    def test_strip_accents(self) -> None:
        assert normalize_text_for_comparison("São Paulo") == "sao paulo"

    def test_strip_accents_portuguese(self) -> None:
        assert normalize_text_for_comparison("João") == "joao"
