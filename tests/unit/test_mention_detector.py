"""Unit tests for deterministic mention detection engine."""

from __future__ import annotations

from app.core.enums import EntityMatchType
from app.services.detection.mention_detector import (
    WarningCode,
    build_entity_terms,
    detect_mentions,
)


def _snapshots(
    brand_name: str = "Acme",
    brand_domain: str = "acme.com",
    brand_aliases: list[str] | None = None,
    competitors: list[dict] | None = None,
) -> list[dict]:
    snaps = [
        {
            "entity_snapshot_id": "brand-1",
            "name": brand_name,
            "domain": brand_domain,
            "aliases": brand_aliases or [],
        }
    ]
    for i, comp in enumerate(competitors or []):
        snaps.append(
            {
                "entity_snapshot_id": f"comp-{i+1}",
                "name": comp["name"],
                "domain": comp["domain"],
                "aliases": comp.get("aliases", []),
            }
        )
    return snaps


class TestBuildEntityTerms:
    def test_brand_name_term(self) -> None:
        terms, warnings = build_entity_terms(_snapshots())
        name_terms = [t for t in terms if t.match_type == EntityMatchType.NAME]
        assert len(name_terms) == 1
        assert name_terms[0].normalized == "acme"
        assert warnings == []

    def test_brand_alias_terms(self) -> None:
        terms, _ = build_entity_terms(_snapshots(brand_aliases=["Acme CRM", "ACME Inc"]))
        alias_terms = [t for t in terms if t.match_type == EntityMatchType.ALIAS]
        assert {t.normalized for t in alias_terms} == {"acme crm", "acme inc"}

    def test_brand_domain_term(self) -> None:
        terms, _ = build_entity_terms(_snapshots())
        domain_terms = [t for t in terms if t.match_type == EntityMatchType.DOMAIN]
        assert len(domain_terms) == 1
        assert domain_terms[0].normalized == "acme.com"

    def test_duplicate_alias_dedup_case_insensitive(self) -> None:
        """Acme and ACME as aliases should deduplicate."""
        terms, _ = build_entity_terms(
            _snapshots(brand_name="Acme", brand_aliases=["Acme", "ACME", "acme"])
        )
        name_alias_terms = [
            t for t in terms if t.match_type in (EntityMatchType.NAME, EntityMatchType.ALIAS)
        ]
        normalized = {t.normalized for t in name_alias_terms}
        assert "acme" in normalized
        assert len([t for t in name_alias_terms if t.normalized == "acme"]) == 1

    def test_ambiguous_alias_excluded_with_warning(self) -> None:
        """When brand and competitor share an alias, it's excluded."""
        snaps = _snapshots(
            brand_aliases=["AI Pro"],
            competitors=[{"name": "Competitor", "domain": "comp.com", "aliases": ["AI Pro"]}],
        )
        terms, warnings = build_entity_terms(snaps)
        # "AI Pro" should NOT appear in any term
        assert all(t.normalized != "ai pro" for t in terms)
        assert any(w[0] == WarningCode.AMBIGUOUS_ENTITY_TERM.value for w in warnings)

    def test_domains_not_excluded_by_ambiguity(self) -> None:
        """Domains remain separately attributable even if shared."""
        snaps = _snapshots(
            brand_domain="shared.com",
            competitors=[{"name": "Comp", "domain": "shared.com", "aliases": []}],
        )
        terms, _ = build_entity_terms(snaps)
        domain_terms = [t for t in terms if t.match_type == EntityMatchType.DOMAIN]
        assert len(domain_terms) == 2  # both entities keep their domain


class TestDetectMentions:
    def test_brand_exact_case_insensitive(self) -> None:
        terms, _ = build_entity_terms(_snapshots())
        result = detect_mentions("I recommend ACME for your CRM needs.", terms)
        assert len(result.mentions) == 1
        assert result.mentions[0].match_type == EntityMatchType.NAME
        assert result.mentions[0].matched_text == "ACME"
        assert result.mentions[0].start_index < result.mentions[0].end_index

    def test_brand_alias_match(self) -> None:
        terms, _ = build_entity_terms(_snapshots(brand_aliases=["Acme CRM"]))
        result = detect_mentions("Acme CRM is great.", terms)
        assert len(result.mentions) == 1
        assert result.mentions[0].match_type == EntityMatchType.ALIAS
        assert result.mentions[0].matched_text == "Acme CRM"

    def test_brand_domain_match(self) -> None:
        terms, _ = build_entity_terms(_snapshots())
        result = detect_mentions("Visit acme.com for more info.", terms)
        assert len(result.mentions) == 1
        assert result.mentions[0].match_type == EntityMatchType.DOMAIN

    def test_competitor_name_match(self) -> None:
        snaps = _snapshots(competitors=[{"name": "Salesforce", "domain": "salesforce.com"}])
        terms, _ = build_entity_terms(snaps)
        result = detect_mentions("Salesforce is a competitor.", terms)
        comp_mentions = [m for m in result.mentions if m.entity_snapshot_id == "comp-1"]
        assert len(comp_mentions) == 1

    def test_no_substring_false_positive(self) -> None:
        """Acme must NOT match Acmeology."""
        terms, _ = build_entity_terms(_snapshots())
        result = detect_mentions("Acmeology is a different company.", terms)
        assert len(result.mentions) == 0

    def test_no_substring_false_positive_notion(self) -> None:
        """Notion must NOT match Notional."""
        terms, _ = build_entity_terms(_snapshots(brand_name="Notion", brand_domain="notion.so"))
        result = detect_mentions("That is a notional idea.", terms)
        assert len(result.mentions) == 0

    def test_multi_word_whitespace_variants(self) -> None:
        """Acme CRM should match Acme   CRM (extra spaces)."""
        terms, _ = build_entity_terms(_snapshots(brand_aliases=["Acme CRM"]))
        result = detect_mentions("Acme   CRM works well.", terms)
        assert len(result.mentions) == 1
        assert result.mentions[0].matched_text == "Acme   CRM"

    def test_overlapping_name_alias_longest_wins(self) -> None:
        """Acme and Acme CRM: longest match wins for same entity."""
        terms, _ = build_entity_terms(_snapshots(brand_name="Acme", brand_aliases=["Acme CRM"]))
        result = detect_mentions("Acme CRM is useful.", terms)
        # Should produce ONE mention for the entity, not two overlapping
        assert len(result.mentions) == 1
        assert result.mentions[0].matched_text == "Acme CRM"

    def test_multiple_genuine_occurrences(self) -> None:
        """Multiple distinct occurrences of the same entity are preserved."""
        terms, _ = build_entity_terms(_snapshots())
        result = detect_mentions("Acme is great. I love Acme.", terms)
        assert len(result.mentions) == 2
        assert result.mentions[0].start_index < result.mentions[1].start_index

    def test_original_character_spans_correct(self) -> None:
        """Start/end indices point to the exact substring in the original text."""
        terms, _ = build_entity_terms(_snapshots())
        text = "I recommend Acme for CRM."
        result = detect_mentions(text, terms)
        assert len(result.mentions) == 1
        m = result.mentions[0]
        assert text[m.start_index : m.end_index] == "Acme"

    def test_empty_response_text(self) -> None:
        terms, _ = build_entity_terms(_snapshots())
        result = detect_mentions("", terms)
        assert result.mentions == []

    def test_no_match(self) -> None:
        terms, _ = build_entity_terms(_snapshots())
        result = detect_mentions("No brand mentioned here.", terms)
        assert result.mentions == []

    def test_case_insensitive_matching(self) -> None:
        """acme, ACME, Acme all match the same entity."""
        terms, _ = build_entity_terms(_snapshots())
        for variant in ["acme", "ACME", "Acme", "aCmE"]:
            result = detect_mentions(f"I use {variant}.", terms)
            assert len(result.mentions) == 1, f"Failed for variant: {variant}"

    def test_domain_with_www_prefix(self) -> None:
        """www.acme.com in text should match the acme.com domain."""
        terms, _ = build_entity_terms(_snapshots())
        result = detect_mentions("Check www.acme.com today.", terms)
        assert len(result.mentions) == 1
        assert result.mentions[0].match_type == EntityMatchType.DOMAIN

    def test_domain_with_url_scheme(self) -> None:
        """https://acme.com in text should match the domain."""
        terms, _ = build_entity_terms(_snapshots())
        result = detect_mentions("See https://acme.com/page for info.", terms)
        assert len(result.mentions) == 1
        assert result.mentions[0].match_type == EntityMatchType.DOMAIN

    def test_different_entities_can_overlap(self) -> None:
        """Different entities can have overlapping text spans."""
        snaps = _snapshots(
            brand_name="Acme",
            competitors=[{"name": "Acme Pro", "domain": "acmepro.com"}],
        )
        terms, _ = build_entity_terms(snaps)
        # "Acme Pro" contains "Acme" — both should be detected since
        # they belong to different entities.
        result = detect_mentions("Acme Pro is a rival.", terms)
        # Both Acme (brand) and Acme Pro (competitor) should match
        # but they overlap. Since they're different entities, both
        # are accepted.
        entity_ids = {m.entity_snapshot_id for m in result.mentions}
        assert "brand-1" in entity_ids
        assert "comp-1" in entity_ids
