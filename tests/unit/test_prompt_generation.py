"""Tests for deterministic prompt generation service."""

from __future__ import annotations

import pytest

from app.core.enums import FunnelStage, PromptType
from app.core.exceptions import ValidationError
from app.core.normalization import normalize_text_for_comparison
from app.models.project import Project
from app.models.tracking import Competitor, ProjectKeyword
from app.services.prompt_generation_service import (
    GENERATOR_KEY,
    PROMPTS_PER_KEYWORD,
    PromptGenerationService,
)


def _make_project(
    brand_name: str = "Acme",
    brand_aliases: list[str] | None = None,
    target_country: str = "US",
    target_language: str = "en",
    target_audience: str | None = "Small Businesses",
    domain: str = "acme.com",
) -> Project:
    return Project(
        name="Test Project",
        domain=domain,
        brand_name=brand_name,
        brand_aliases=brand_aliases or [],
        target_country=target_country,
        target_language=target_language,
        target_audience=target_audience,
        prompt_input_revision=1,
    )


def _make_keyword(
    text: str = "best crm",
    funnel_stage: FunnelStage | None = FunnelStage.PURCHASE,
    intent: str | None = "buying",
) -> ProjectKeyword:
    import uuid

    kw = ProjectKeyword(
        text=text,
        normalized_text=text.lower(),
        funnel_stage=funnel_stage,
        intent=intent,
        active=True,
    )
    kw.id = uuid.uuid4()
    return kw


def _make_competitor(name: str = "Mailchimp", domain: str = "mailchimp.com") -> Competitor:
    import uuid

    comp = Competitor(name=name, domain=domain, active=True)
    comp.id = uuid.uuid4()
    return comp


@pytest.mark.unit
class TestPromptGeneration:
    def test_exactly_5_prompts_per_keyword(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword()]
        competitors = [_make_competitor()]

        specs = svc.generate_prompts(project, keywords, competitors)
        assert len(specs) == PROMPTS_PER_KEYWORD

    def test_5_prompts_per_keyword_multiple_keywords(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword("best crm"), _make_keyword("email marketing")]
        competitors = [_make_competitor()]

        specs = svc.generate_prompts(project, keywords, competitors)
        assert len(specs) == 10  # 2 keywords * 5 prompts

    def test_variant_distribution_with_competitors(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword()]
        competitors = [_make_competitor()]

        specs = svc.generate_prompts(project, keywords, competitors)
        types = [s.prompt_type for s in specs]
        assert types == [
            PromptType.NON_BRANDED,
            PromptType.NON_BRANDED,
            PromptType.NON_BRANDED,
            PromptType.BRANDED,
            PromptType.COMPETITOR,
        ]

    def test_variant_distribution_without_competitors(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        types = [s.prompt_type for s in specs]
        assert types == [
            PromptType.NON_BRANDED,
            PromptType.NON_BRANDED,
            PromptType.NON_BRANDED,
            PromptType.BRANDED,
            PromptType.NON_BRANDED,
        ]

    def test_variant_indices_1_to_5(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword()]
        competitors = [_make_competitor()]

        specs = svc.generate_prompts(project, keywords, competitors)
        indices = [s.variant_index for s in specs]
        assert indices == [1, 2, 3, 4, 5]

    def test_no_active_keywords_raises(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword()]
        keywords[0].active = False
        competitors: list[Competitor] = []

        with pytest.raises(ValidationError, match="no active keywords"):
            svc.generate_prompts(project, keywords, competitors)

    def test_empty_keywords_raises(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords: list[ProjectKeyword] = []
        competitors: list[Competitor] = []

        with pytest.raises(ValidationError, match="no active keywords"):
            svc.generate_prompts(project, keywords, competitors)


@pytest.mark.unit
class TestNonBrandedSafety:
    """NON_BRANDED prompts MUST NOT contain brand name, aliases, or competitor references."""

    def test_non_branded_excludes_brand_name(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(brand_name="Acme")
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]

        for spec in non_branded:
            assert "acme" not in normalize_text_for_comparison(spec.text), (
                f"NON_BRANDED prompt contains brand name: {spec.text}"
            )

    def test_non_branded_excludes_brand_aliases(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(brand_name="Acme", brand_aliases=["Acme Inc", "AcmeCorp"])
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]

        for spec in non_branded:
            text_lower = normalize_text_for_comparison(spec.text)
            assert "acme inc" not in text_lower
            assert "acmecorp" not in text_lower

    def test_non_branded_excludes_competitor_names(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(brand_name="Acme")
        keywords = [_make_keyword()]
        competitors = [_make_competitor(name="Mailchimp", domain="mailchimp.com")]

        specs = svc.generate_prompts(project, keywords, competitors)
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]

        for spec in non_branded:
            text_lower = normalize_text_for_comparison(spec.text)
            assert "mailchimp" not in text_lower, (
                f"NON_BRANDED prompt contains competitor name: {spec.text}"
            )

    def test_non_branded_excludes_competitor_domains(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(brand_name="Acme")
        keywords = [_make_keyword()]
        competitors = [_make_competitor(name="HubSpot", domain="hubspot.com")]

        specs = svc.generate_prompts(project, keywords, competitors)
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]

        for spec in non_branded:
            text_lower = normalize_text_for_comparison(spec.text)
            assert "hubspot" not in text_lower, (
                f"NON_BRANDED prompt contains competitor domain: {spec.text}"
            )

    def test_branded_prompt_contains_brand(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(brand_name="Acme")
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        branded = [s for s in specs if s.prompt_type == PromptType.BRANDED]

        assert len(branded) == 1
        assert "acme" in normalize_text_for_comparison(branded[0].text)

    def test_competitor_prompt_contains_brand_and_competitor(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(brand_name="Acme")
        keywords = [_make_keyword()]
        competitors = [_make_competitor(name="Mailchimp", domain="mailchimp.com")]

        specs = svc.generate_prompts(project, keywords, competitors)
        comp_specs = [s for s in specs if s.prompt_type == PromptType.COMPETITOR]

        assert len(comp_specs) == 1
        text_lower = normalize_text_for_comparison(comp_specs[0].text)
        assert "acme" in text_lower
        assert "mailchimp" in text_lower


@pytest.mark.unit
class TestPromptMetadata:
    def test_intent_copied_from_keyword(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword(intent="buying")]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        for spec in specs:
            assert spec.intent == "buying"

    def test_funnel_stage_copied_from_keyword(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword(funnel_stage=FunnelStage.AWARENESS)]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        for spec in specs:
            assert spec.funnel_stage == FunnelStage.AWARENESS

    def test_persona_copied_from_project(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(target_audience="Small Businesses")
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        for spec in specs:
            assert spec.persona == "Small Businesses"

    def test_persona_none_when_not_supplied(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(target_audience=None)
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        for spec in specs:
            assert spec.persona is None

    def test_target_country_copied(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(target_country="US")
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        for spec in specs:
            assert spec.target_country == "US"

    def test_target_language_copied(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(target_language="en")
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        for spec in specs:
            assert spec.target_language == "en"

    def test_commercial_intent_purchase_true(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword(funnel_stage=FunnelStage.PURCHASE)]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        for spec in specs:
            assert spec.commercial_intent is True

    def test_commercial_intent_consideration_true(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword(funnel_stage=FunnelStage.CONSIDERATION)]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        for spec in specs:
            assert spec.commercial_intent is True

    def test_commercial_intent_awareness_false(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword(funnel_stage=FunnelStage.AWARENESS)]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        for spec in specs:
            assert spec.commercial_intent is False

    def test_commercial_intent_none_false(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword(funnel_stage=None)]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        for spec in specs:
            assert spec.commercial_intent is False


@pytest.mark.unit
class TestLanguageSupport:
    def test_english_templates(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(target_language="en")
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        assert "best options" in non_branded[0].text.lower()

    def test_english_us_templates(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(target_language="en-us")
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        assert "best options" in non_branded[0].text.lower()

    def test_portuguese_templates(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(target_language="pt", target_country="BR")
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        assert "melhores" in non_branded[0].text.lower()

    def test_portuguese_br_templates(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(target_language="pt-br", target_country="BR")
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        assert "melhores" in non_branded[0].text.lower()

    def test_unsupported_language_raises(self) -> None:
        from app.core.exceptions import ValidationError

        svc = PromptGenerationService()
        project = _make_project(target_language="fr")
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        with pytest.raises(ValidationError, match="not supported"):
            svc.generate_prompts(project, keywords, competitors)


@pytest.mark.unit
class TestPromptLength:
    def test_prompt_length_within_limit(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword()]
        competitors = [_make_competitor()]

        specs = svc.generate_prompts(project, keywords, competitors)
        for spec in specs:
            assert len(spec.text) <= 1000, f"Prompt too long: {len(spec.text)} chars"


@pytest.mark.unit
class TestDeterministicGeneration:
    def test_same_inputs_same_output(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword()]
        competitors = [_make_competitor()]

        specs1 = svc.generate_prompts(project, keywords, competitors)
        specs2 = svc.generate_prompts(project, keywords, competitors)

        assert len(specs1) == len(specs2)
        for s1, s2 in zip(specs1, specs2, strict=False):
            assert s1.text == s2.text
            assert s1.prompt_type == s2.prompt_type
            assert s1.variant_index == s2.variant_index

    def test_generator_key_is_deterministic_v2(self) -> None:
        assert GENERATOR_KEY == "deterministic-template-v2"


@pytest.mark.unit
class TestPromptDistinctness:
    """All 5 prompts per keyword must have distinct text after normalization."""

    def test_all_5_distinct_with_competitor(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword()]
        competitors = [_make_competitor()]

        specs = svc.generate_prompts(project, keywords, competitors)
        assert len(specs) == 5

        normalized_texts = [normalize_text_for_comparison(s.text) for s in specs]
        assert len(set(normalized_texts)) == 5, f"Duplicate prompts found: {normalized_texts}"

    def test_all_5_distinct_without_competitor(self) -> None:
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        assert len(specs) == 5

        normalized_texts = [normalize_text_for_comparison(s.text) for s in specs]
        assert len(set(normalized_texts)) == 5, f"Duplicate prompts found: {normalized_texts}"

    def test_all_5_distinct_english(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(target_language="en", target_country="US")
        keywords = [_make_keyword()]
        competitors = [_make_competitor()]

        specs = svc.generate_prompts(project, keywords, competitors)
        normalized_texts = [normalize_text_for_comparison(s.text) for s in specs]
        assert len(set(normalized_texts)) == 5

    def test_all_5_distinct_portuguese(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(target_language="pt", target_country="BR")
        keywords = [_make_keyword()]
        competitors = [_make_competitor()]

        specs = svc.generate_prompts(project, keywords, competitors)
        normalized_texts = [normalize_text_for_comparison(s.text) for s in specs]
        assert len(set(normalized_texts)) == 5

    def test_all_5_distinct_portuguese_without_competitor(self) -> None:
        svc = PromptGenerationService()
        project = _make_project(target_language="pt", target_country="BR")
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        normalized_texts = [normalize_text_for_comparison(s.text) for s in specs]
        assert len(set(normalized_texts)) == 5

    def test_distinct_across_multiple_keywords(self) -> None:
        """Each keyword's 5 prompts must be distinct within that keyword."""
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword("best crm"), _make_keyword("email marketing")]
        competitors = [_make_competitor()]

        specs = svc.generate_prompts(project, keywords, competitors)
        assert len(specs) == 10

        # Group by keyword and check distinctness within each group.
        for kw in keywords:
            kw_specs = [s for s in specs if s.keyword_id == kw.id]
            assert len(kw_specs) == 5
            normalized = [normalize_text_for_comparison(s.text) for s in kw_specs]
            assert len(set(normalized)) == 5

    def test_non_branded_variants_use_different_templates(self) -> None:
        """Verify that NON_BRANDED variants 1, 2, 3 produce different text."""
        svc = PromptGenerationService()
        project = _make_project()
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        assert len(non_branded) == 4  # 3 + 1 (variant 5 without competitor)

        # All 4 NON_BRANDED texts must be distinct.
        normalized = [normalize_text_for_comparison(s.text) for s in non_branded]
        assert len(set(normalized)) == 4

    def test_non_branded_safety_all_distinct_variants(self) -> None:
        """All distinct NON_BRANDED variants still exclude brand/aliases/competitors."""
        svc = PromptGenerationService()
        project = _make_project(brand_name="Acme", brand_aliases=["Acme Inc"])
        keywords = [_make_keyword()]
        competitors = [_make_competitor(name="Mailchimp", domain="mailchimp.com")]

        specs = svc.generate_prompts(project, keywords, competitors)
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        assert len(non_branded) == 3

        for spec in non_branded:
            text_lower = normalize_text_for_comparison(spec.text)
            assert "acme" not in text_lower
            assert "acme inc" not in text_lower
            assert "mailchimp" not in text_lower

    def test_non_branded_safety_all_distinct_variants_without_competitor(self) -> None:
        """All 4 NON_BRANDED variants (no competitor) still exclude brand/aliases."""
        svc = PromptGenerationService()
        project = _make_project(brand_name="Acme", brand_aliases=["Acme Inc"])
        keywords = [_make_keyword()]
        competitors: list[Competitor] = []

        specs = svc.generate_prompts(project, keywords, competitors)
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        assert len(non_branded) == 4

        for spec in non_branded:
            text_lower = normalize_text_for_comparison(spec.text)
            assert "acme" not in text_lower
            assert "acme inc" not in text_lower
