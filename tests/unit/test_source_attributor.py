"""Unit tests for deterministic source attribution engine."""

from __future__ import annotations

from app.core.enums import AttributionType
from app.services.detection.source_attributor import (
    attribute_source,
    build_entity_domains,
    parse_source_host,
)


class TestParseSourceHost:
    def test_https_url(self) -> None:
        assert parse_source_host("https://acme.com/page") == "acme.com"

    def test_http_url(self) -> None:
        assert parse_source_host("http://acme.com") == "acme.com"

    def test_url_with_www(self) -> None:
        assert parse_source_host("https://www.acme.com/page") == "www.acme.com"

    def test_url_with_port(self) -> None:
        assert parse_source_host("https://acme.com:8080/page") == "acme.com"

    def test_url_without_scheme(self) -> None:
        assert parse_source_host("acme.com/page") == "acme.com"

    def test_url_with_subdomain(self) -> None:
        assert parse_source_host("https://blog.acme.com/post") == "blog.acme.com"

    def test_empty_url(self) -> None:
        assert parse_source_host("") is None

    def test_none_url(self) -> None:
        assert parse_source_host(None) is None  # type: ignore[arg-type]

    def test_invalid_url(self) -> None:
        assert parse_source_host("not a url at all") is None

    def test_url_no_host(self) -> None:
        assert parse_source_host("://nohost") is None

    def test_uppercase_host_normalized(self) -> None:
        assert parse_source_host("https://ACME.COM") == "acme.com"

    def test_trailing_dot_stripped(self) -> None:
        assert parse_source_host("https://acme.com./page") == "acme.com"


class TestAttributeSource:
    def _domains(self) -> list:
        return build_entity_domains(
            [
                {"entity_snapshot_id": "brand-1", "domain": "acme.com"},
                {"entity_snapshot_id": "comp-1", "domain": "salesforce.com"},
            ]
        )

    def test_exact_domain_match(self) -> None:
        attr = attribute_source("acme.com", self._domains())
        assert attr is not None
        assert attr.entity_snapshot_id == "brand-1"
        assert attr.source_host == "acme.com"
        assert attr.attribution_type == AttributionType.OWNED_DOMAIN.value

    def test_subdomain_match(self) -> None:
        attr = attribute_source("blog.acme.com", self._domains())
        assert attr is not None
        assert attr.entity_snapshot_id == "brand-1"

    def test_www_subdomain_match(self) -> None:
        attr = attribute_source("www.acme.com", self._domains())
        assert attr is not None
        assert attr.entity_snapshot_id == "brand-1"

    def test_no_match(self) -> None:
        attr = attribute_source("example.com", self._domains())
        assert attr is None

    def test_most_specific_domain_wins(self) -> None:
        """When both acme.com and product.acme.com are tracked, the
        more specific one wins for a product.acme.com source.
        """
        domains = build_entity_domains(
            [
                {"entity_snapshot_id": "brand-1", "domain": "acme.com"},
                {"entity_snapshot_id": "sub-1", "domain": "product.acme.com"},
            ]
        )
        attr = attribute_source("product.acme.com", domains)
        assert attr is not None
        assert attr.entity_snapshot_id == "sub-1"

    def test_most_specific_domain_wins_deep(self) -> None:
        """blog.product.acme.com should match product.acme.com, not acme.com."""
        domains = build_entity_domains(
            [
                {"entity_snapshot_id": "brand-1", "domain": "acme.com"},
                {"entity_snapshot_id": "sub-1", "domain": "product.acme.com"},
            ]
        )
        attr = attribute_source("blog.product.acme.com", domains)
        assert attr is not None
        assert attr.entity_snapshot_id == "sub-1"

    def test_no_naive_substring_match(self) -> None:
        """evilacme.com must NOT match acme.com."""
        attr = attribute_source("evilacme.com", self._domains())
        assert attr is None

    def test_no_naive_suffix_match(self) -> None:
        """notacme.com must NOT match acme.com."""
        attr = attribute_source("notacme.com", self._domains())
        assert attr is None

    def test_empty_host(self) -> None:
        attr = attribute_source("", self._domains())
        assert attr is None

    def test_competitor_domain_match(self) -> None:
        attr = attribute_source("salesforce.com", self._domains())
        assert attr is not None
        assert attr.entity_snapshot_id == "comp-1"
