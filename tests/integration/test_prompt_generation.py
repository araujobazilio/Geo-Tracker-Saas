"""Tests for prompt generation service — deterministic templates, NON_BRANDED safety."""

from __future__ import annotations

import uuid

import pytest

from app.core.enums import FunnelStage, ProjectStatus, PromptType, WorkspaceType
from app.core.exceptions import ValidationError
from app.models import Project, User, Workspace, WorkspaceMember
from app.models.tracking import Competitor, ProjectKeyword
from app.services.prompt_generation_service import (
    GENERATOR_KEY,
    PROMPTS_PER_KEYWORD,
    PromptGenerationService,
)


def _make_project(
    db_session,  # type: ignore[no-untyped-def]
    brand_name: str = "Acme",
    brand_aliases: list[str] | None = None,
    target_language: str | None = "en",
    target_country: str | None = "US",
    target_audience: str | None = "Small Businesses",
) -> Project:
    user = User(email=f"pg-{uuid.uuid4().hex[:8]}@example.com", password_hash="h")
    ws = Workspace(name=f"PG WS {uuid.uuid4().hex[:4]}", workspace_type=WorkspaceType.PERSONAL)
    db_session.add_all([user, ws])
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="OWNER"))
    db_session.flush()
    project = Project(
        workspace_id=ws.id,
        name=brand_name,
        domain=f"{brand_name.lower()}.example",
        brand_name=brand_name,
        brand_aliases=brand_aliases or [],
        target_country=target_country,
        target_language=target_language,
        target_audience=target_audience,
        status=ProjectStatus.ACTIVE,
        prompt_input_revision=1,
    )
    db_session.add(project)
    db_session.flush()
    return project


def _make_keyword(
    project: Project, text: str, funnel_stage: FunnelStage | None = None
) -> ProjectKeyword:
    return ProjectKeyword(
        project_id=project.id,
        text=text,
        normalized_text=text.lower(),
        funnel_stage=funnel_stage,
        active=True,
    )


def _make_competitor(project: Project, name: str, domain: str) -> Competitor:
    return Competitor(
        project_id=project.id,
        name=name,
        domain=domain,
        active=True,
    )


@pytest.mark.integration
class TestPromptGeneration:
    def test_exactly_5_prompts_per_keyword(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session)
        kw = _make_keyword(project, "best crm")
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])
        assert len(specs) == PROMPTS_PER_KEYWORD

    def test_5_prompts_per_keyword_with_multiple_keywords(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session)
        kw1 = _make_keyword(project, "best crm")
        kw2 = _make_keyword(project, "email marketing")
        kw3 = _make_keyword(project, "lead generation")
        db_session.add_all([kw1, kw2, kw3])
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw1, kw2, kw3], [])
        assert len(specs) == 15  # 3 keywords * 5 prompts

    def test_3_non_branded_with_competitor(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session)
        kw = _make_keyword(project, "best crm")
        comp = _make_competitor(project, "Salesforce", "salesforce.com")
        db_session.add_all([kw, comp])
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [comp])

        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        branded = [s for s in specs if s.prompt_type == PromptType.BRANDED]
        competitor = [s for s in specs if s.prompt_type == PromptType.COMPETITOR]

        assert len(non_branded) == 3  # 3 NON_BRANDED variants
        assert len(branded) == 1
        assert len(competitor) == 1

    def test_branded_variant_exists(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session, brand_name="Acme")
        kw = _make_keyword(project, "best crm")
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        branded = [s for s in specs if s.prompt_type == PromptType.BRANDED]
        assert len(branded) == 1
        assert "Acme" in branded[0].text

    def test_competitor_variant_with_competitors(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session, brand_name="Acme")
        kw = _make_keyword(project, "best crm")
        comp = _make_competitor(project, "Salesforce", "salesforce.com")
        db_session.add_all([kw, comp])
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [comp])

        competitor_specs = [s for s in specs if s.prompt_type == PromptType.COMPETITOR]
        assert len(competitor_specs) == 1
        assert "Acme" in competitor_specs[0].text
        assert "Salesforce" in competitor_specs[0].text

    def test_without_competitor_5th_is_non_branded(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session)
        kw = _make_keyword(project, "best crm")
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        assert len(specs) == 5
        assert all(s.variant_index == i + 1 for i, s in enumerate(specs))
        # Without competitors: 4 NON_BRANDED + 1 BRANDED
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        branded = [s for s in specs if s.prompt_type == PromptType.BRANDED]
        assert len(non_branded) == 4
        assert len(branded) == 1

    def test_non_branded_does_not_contain_brand_name(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session, brand_name="Acme")
        kw = _make_keyword(project, "best crm")
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        for spec in non_branded:
            assert "acme" not in spec.text.lower(), (
                f"NON_BRANDED prompt contains brand name: {spec.text}"
            )

    def test_non_branded_does_not_contain_brand_aliases(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(
            db_session, brand_name="Acme", brand_aliases=["Acme Inc", "AcmeCorp"]
        )
        kw = _make_keyword(project, "best crm")
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        for spec in non_branded:
            text_lower = spec.text.lower()
            assert "acme inc" not in text_lower
            assert "acmecorp" not in text_lower

    def test_non_branded_does_not_contain_competitor_names(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session, brand_name="Acme")
        kw = _make_keyword(project, "best crm")
        comp = _make_competitor(project, "Salesforce", "salesforce.com")
        db_session.add_all([kw, comp])
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [comp])

        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        for spec in non_branded:
            assert "salesforce" not in spec.text.lower(), (
                f"NON_BRANDED prompt contains competitor name: {spec.text}"
            )

    def test_non_branded_does_not_contain_competitor_domains(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session, brand_name="Acme")
        kw = _make_keyword(project, "best crm")
        comp = _make_competitor(project, "Mailchimp", "mailchimp.com")
        db_session.add_all([kw, comp])
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [comp])

        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        for spec in non_branded:
            text_lower = spec.text.lower()
            assert "mailchimp.com" not in text_lower
            assert "mailchimp" not in text_lower

    def test_metadata_snapshots_correctly(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(
            db_session,
            target_country="US",
            target_language="en",
            target_audience="Small Businesses",
        )
        kw = _make_keyword(project, "best crm", funnel_stage=FunnelStage.PURCHASE)
        kw.intent = "buying"
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        for spec in specs:
            assert spec.target_country == "US"
            assert spec.target_language == "en"
            assert spec.persona == "Small Businesses"
            assert spec.intent == "buying"
            assert spec.funnel_stage == FunnelStage.PURCHASE
            assert spec.commercial_intent is True  # PURCHASE → True

    def test_commercial_intent_mapping(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session)
        kw_purchase = _make_keyword(project, "buy crm", FunnelStage.PURCHASE)
        kw_consideration = _make_keyword(project, "crm comparison", FunnelStage.CONSIDERATION)
        kw_awareness = _make_keyword(project, "what is crm", FunnelStage.AWARENESS)
        db_session.add_all([kw_purchase, kw_consideration, kw_awareness])
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw_purchase, kw_consideration, kw_awareness], [])

        purchase_specs = [s for s in specs if s.keyword_id == kw_purchase.id]
        consideration_specs = [s for s in specs if s.keyword_id == kw_consideration.id]
        awareness_specs = [s for s in specs if s.keyword_id == kw_awareness.id]

        assert all(s.commercial_intent is True for s in purchase_specs)
        assert all(s.commercial_intent is True for s in consideration_specs)
        assert all(s.commercial_intent is False for s in awareness_specs)

    def test_commercial_intent_default_false_for_no_funnel(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session)
        kw = _make_keyword(project, "best crm", funnel_stage=None)
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        assert all(s.commercial_intent is False for s in specs)

    def test_persona_copied_when_supplied(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session, target_audience="SMB Owners")
        kw = _make_keyword(project, "best crm")
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        assert all(s.persona == "SMB Owners" for s in specs)

    def test_persona_none_when_not_supplied(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session, target_audience=None)
        kw = _make_keyword(project, "best crm")
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        assert all(s.persona is None for s in specs)

    def test_english_templates(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session, target_language="en")
        kw = _make_keyword(project, "best crm")
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        # Check that English template text is used.
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        assert any("best options" in s.text.lower() for s in non_branded)

    def test_portuguese_templates(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session, target_language="pt-br", target_country="BR")
        kw = _make_keyword(project, "melhor crm")
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        # Check that Portuguese template text is used.
        non_branded = [s for s in specs if s.prompt_type == PromptType.NON_BRANDED]
        assert any("melhores" in s.text.lower() for s in non_branded)

    def test_unsupported_language_raises(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session, target_language=None)
        # Manually set unsupported language after creation.
        project.target_language = "fr"
        kw = _make_keyword(project, "best crm")
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        with pytest.raises(ValidationError, match="not supported"):
            svc.generate_prompts(project, [kw], [])

    def test_prompt_length_within_limit(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session)
        # Very long keyword text.
        kw = _make_keyword(project, "a" * 400)
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        for spec in specs:
            assert len(spec.text) <= 1000, (
                f"Prompt exceeds 1000 chars ({len(spec.text)}): {spec.text[:100]}"
            )

    def test_deterministic_generation_same_inputs(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session, brand_name="Acme")
        kw = _make_keyword(project, "best crm")
        comp = _make_competitor(project, "Salesforce", "salesforce.com")
        db_session.add_all([kw, comp])
        db_session.flush()

        svc = PromptGenerationService()
        specs1 = svc.generate_prompts(project, [kw], [comp])
        specs2 = svc.generate_prompts(project, [kw], [comp])

        assert len(specs1) == len(specs2)
        for s1, s2 in zip(specs1, specs2, strict=False):
            assert s1.text == s2.text
            assert s1.prompt_type == s2.prompt_type
            assert s1.variant_index == s2.variant_index

    def test_generator_key_is_v2(self) -> None:
        assert GENERATOR_KEY == "deterministic-template-v2"

    def test_all_5_distinct_with_competitor(self, db_session) -> None:  # type: ignore[no-untyped-def]
        from app.core.normalization import normalize_text_for_comparison

        project = _make_project(db_session)
        kw = _make_keyword(project, "best crm")
        comp = _make_competitor(project, "Salesforce", "salesforce.com")
        db_session.add_all([kw, comp])
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [comp])

        normalized = [normalize_text_for_comparison(s.text) for s in specs]
        assert len(set(normalized)) == 5

    def test_all_5_distinct_without_competitor(self, db_session) -> None:  # type: ignore[no-untyped-def]
        from app.core.normalization import normalize_text_for_comparison

        project = _make_project(db_session)
        kw = _make_keyword(project, "best crm")
        db_session.add(kw)
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [])

        normalized = [normalize_text_for_comparison(s.text) for s in specs]
        assert len(set(normalized)) == 5

    def test_all_5_distinct_portuguese(self, db_session) -> None:  # type: ignore[no-untyped-def]
        from app.core.normalization import normalize_text_for_comparison

        project = _make_project(db_session, target_language="pt-br", target_country="BR")
        kw = _make_keyword(project, "melhor crm")
        comp = _make_competitor(project, "Salesforce", "salesforce.com")
        db_session.add_all([kw, comp])
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw], [comp])

        normalized = [normalize_text_for_comparison(s.text) for s in specs]
        assert len(set(normalized)) == 5

    def test_no_active_keywords_raises(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session)
        # No keywords at all.
        svc = PromptGenerationService()
        with pytest.raises(ValidationError, match="no active keywords"):
            svc.generate_prompts(project, [], [])

    def test_inactive_keywords_not_generated(self, db_session) -> None:  # type: ignore[no-untyped-def]
        project = _make_project(db_session)
        kw_active = _make_keyword(project, "best crm")
        kw_inactive = _make_keyword(project, "email marketing")
        kw_inactive.active = False
        db_session.add_all([kw_active, kw_inactive])
        db_session.flush()

        svc = PromptGenerationService()
        specs = svc.generate_prompts(project, [kw_active, kw_inactive], [])

        # Only active keyword generates prompts.
        assert len(specs) == 5
        assert all(s.keyword_id == kw_active.id for s in specs)
