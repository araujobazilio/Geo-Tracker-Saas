"""Prompt generation service — deterministic template-based prompt generation.

This service does NOT call external AI APIs. It uses deterministic
templates to generate stable prompts from project configuration.

Generator key: deterministic-template-v2

For each active keyword, exactly 5 prompt variants are generated,
each with a DISTINCT prompt text (no duplicates):
  Variant 1: NON_BRANDED — recommendations
  Variant 2: NON_BRANDED — comparison / shortlist
  Variant 3: NON_BRANDED — decision criteria
  Variant 4: BRANDED
  Variant 5: COMPETITOR (if active competitors exist) or NON_BRANDED — buyer-oriented

NON_BRANDED prompts MUST NOT contain the brand name, brand aliases,
competitor names, or competitor domains. This is critical for
Share-of-Voice measurement.

Supported languages:
  - English (en, en-US, en-GB)
  - Portuguese (pt, pt-BR, pt-PT)

Unsupported languages raise ValidationError.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.enums import FunnelStage, PromptType
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.core.normalization import (
    get_language_family,
    normalize_text_for_comparison,
)
from app.models.project import Project
from app.models.tracking import Competitor, ProjectKeyword, Prompt

logger = get_logger("app.prompt_generation")

GENERATOR_KEY = "deterministic-template-v2"
MAX_PROMPT_LENGTH = 1000
PROMPTS_PER_KEYWORD = 5


@dataclass(frozen=True)
class GeneratedPromptSpec:
    """Specification for a single generated prompt.

    The service produces these specs; PromptSetService persists them.
    """

    keyword_id: uuid.UUID
    variant_index: int
    prompt_type: PromptType
    text: str
    intent: str | None
    funnel_stage: FunnelStage | None
    persona: str | None
    target_country: str | None
    target_language: str | None
    commercial_intent: bool


def _commercial_intent_from_funnel(funnel_stage: FunnelStage | None) -> bool:
    """Determine commercial intent from funnel stage.

    PURCHASE → True
    CONSIDERATION → True
    AWARENESS → False
    None (unknown) → False (conservative default)
    """
    return funnel_stage in (FunnelStage.PURCHASE, FunnelStage.CONSIDERATION)


def _market_context(project: Project, family: str) -> str:
    """Build a market context string for templates."""
    country = project.target_country or ""
    if family == "pt":
        if country:
            return f"no {country}"
        return "no mercado"
    if country:
        return f"in {country}"
    return "in the market"


def _build_non_branded_prompt_variant(
    variant: int,
    keyword_text: str,
    project: Project,
    family: str,
    market: str,
) -> str:
    """Build a NON_BRANDED prompt for a specific variant index.

    Each variant represents a different natural search behavior:
      1 — recommendations
      2 — comparison / shortlist
      3 — decision criteria
      5 — buyer-oriented alternative (used when no competitor for variant 5)

    MUST NOT contain brand name, brand aliases, competitor names, or
    competitor domains.
    """
    if family == "pt":
        if variant == 1:
            return (
                f"Quais são as melhores opções de {keyword_text} {market}? "
                f"Por favor, liste e compare as principais alternativas."
            )
        if variant == 2:
            return (
                f"Quais soluções de {keyword_text} devo comparar {market}? "
                f"Quais são as principais diferenças entre elas?"
            )
        if variant == 3:
            return (
                f"O que devo considerar ao escolher {keyword_text} {market}? "
                f"Quais opções se destacam e por quê?"
            )
        # variant == 5 (buyer-oriented, no competitor available)
        return (
            f"Se eu fosse escolher {keyword_text} {market} hoje, "
            f"quais opções você listaria e por quê?"
        )

    # English templates
    if variant == 1:
        return (
            f"What are the best options for {keyword_text} {market}? "
            f"Please list and compare the top alternatives."
        )
    if variant == 2:
        return (
            f"Which {keyword_text} solutions should I compare {market}, "
            f"and what are the main differences between them?"
        )
    if variant == 3:
        return (
            f"What should I look for when choosing {keyword_text} {market}, "
            f"and which options stand out?"
        )
    # variant == 5 (buyer-oriented, no competitor available)
    return (
        f"If I were choosing {keyword_text} {market} today, "
        f"which options would you shortlist and why?"
    )


def _build_branded_prompt(
    keyword_text: str,
    brand_name: str,
    project: Project,
    family: str,
    market: str,
) -> str:
    """Build a BRANDED prompt (includes the primary brand)."""
    if family == "pt":
        return (
            f"O {brand_name} é uma boa opção para {keyword_text} {market}? "
            f"Quais os pontos fortes e alternativas devo considerar?"
        )
    return (
        f"Is {brand_name} a good option for {keyword_text} {market}? "
        f"What strengths and alternatives should I consider?"
    )


def _build_competitor_prompt(
    keyword_text: str,
    brand_name: str,
    competitors: list[Competitor],
    project: Project,
    family: str,
    market: str,
) -> str:
    """Build a COMPETITOR comparison prompt.

    Uses up to 3 active competitors (deterministic: first by created_at/id).
    """
    # Deterministic subset: first 3 active competitors ordered by name.
    selected = sorted(competitors, key=lambda c: (c.name, str(c.id)))[:3]
    competitor_names = [c.name for c in selected]

    if family == "pt":
        if len(competitor_names) == 1:
            comp_str = competitor_names[0]
            return (
                f"Compare {brand_name} e {comp_str} para {keyword_text} {market}. "
                f"Quais as principais diferenças e qual recomendaria?"
            )
        comp_list = " e ".join(competitor_names)
        return (
            f"Compare {brand_name}, {comp_list} para {keyword_text} {market}. "
            f"Quais as principais diferenças e qual recomendaria?"
        )
    if len(competitor_names) == 1:
        comp_str = competitor_names[0]
        return (
            f"Compare {brand_name} and {comp_str} for {keyword_text} {market}. "
            f"What are the main differences and which would you recommend?"
        )
    comp_list = " and ".join(competitor_names)
    return (
        f"Compare {brand_name}, {comp_list} for {keyword_text} {market}. "
        f"What are the main differences and which would you recommend?"
    )


def _validate_non_branded_safety(
    text: str,
    brand_name: str,
    brand_aliases: list[str],
    competitors: list[Competitor],
) -> None:
    """Ensure NON_BRANDED prompt text contains no brand/competitor references.

    Uses accent-insensitive, case-insensitive comparison.
    """
    text_lower = normalize_text_for_comparison(text)

    # Check brand name.
    if normalize_text_for_comparison(brand_name) in text_lower:
        raise ValidationError(f"NON_BRANDED prompt contains brand name '{brand_name}': {text}")

    # Check brand aliases.
    for alias in brand_aliases:
        if normalize_text_for_comparison(alias) in text_lower:
            raise ValidationError(f"NON_BRANDED prompt contains brand alias '{alias}': {text}")

    # Check competitor names.
    for comp in competitors:
        if normalize_text_for_comparison(comp.name) in text_lower:
            raise ValidationError(
                f"NON_BRANDED prompt contains competitor name '{comp.name}': {text}"
            )

    # Check competitor domains (base hostname without TLD parts).
    for comp in competitors:
        domain = comp.domain.lower()
        # Check the full domain and the main part.
        if domain and domain in text_lower:
            raise ValidationError(
                f"NON_BRANDED prompt contains competitor domain '{domain}': {text}"
            )
        # Check the primary domain label (e.g. "mailchimp" from "mailchimp.com").
        parts = domain.split(".")
        if parts and len(parts[0]) > 3:
            label = normalize_text_for_comparison(parts[0])
            if label and label in text_lower:
                raise ValidationError(
                    f"NON_BRANDED prompt contains competitor domain label " f"'{parts[0]}': {text}"
                )


class PromptGenerationService:
    """Deterministic prompt generation from project configuration.

    This service does NOT call external AI APIs. It produces stable
    prompt text from deterministic templates.

    The same inputs + generator_key always produce the same prompt text.
    """

    def __init__(self) -> None:
        pass

    def generate_prompts(
        self,
        project: Project,
        keywords: list[ProjectKeyword],
        competitors: list[Competitor],
    ) -> list[GeneratedPromptSpec]:
        """Generate all prompts for a project's active keywords.

        Produces exactly 5 prompts per active keyword.

        Args:
            project: The project configuration.
            keywords: Active keywords for the project.
            competitors: Active competitors for the project.

        Returns:
            List of GeneratedPromptSpec objects (5 per keyword).

        Raises:
            ValidationError: if no active keywords, unsupported language,
                or NON_BRANDED safety violation.
        """
        active_keywords = [kw for kw in keywords if kw.active]
        if not active_keywords:
            raise ValidationError("Cannot generate prompts: project has no active keywords.")

        active_competitors = [c for c in competitors if c.active]

        family = get_language_family(project.target_language)

        prompts: list[GeneratedPromptSpec] = []

        for keyword in active_keywords:
            keyword_prompts = self._generate_keyword_prompts(
                keyword=keyword,
                project=project,
                competitors=active_competitors,
                family=family,
            )
            prompts.extend(keyword_prompts)

        return prompts

    def _generate_keyword_prompts(
        self,
        keyword: ProjectKeyword,
        project: Project,
        competitors: list[Competitor],
        family: str,
    ) -> list[GeneratedPromptSpec]:
        """Generate exactly 5 prompts for a single keyword."""
        market = _market_context(project, family)
        brand_name = project.brand_name
        brand_aliases = project.brand_aliases
        persona = project.target_audience
        target_country = project.target_country
        target_language = project.target_language
        funnel = keyword.funnel_stage
        intent = keyword.intent
        commercial = _commercial_intent_from_funnel(funnel)

        specs: list[GeneratedPromptSpec] = []

        # Variants 1-3: NON_BRANDED (distinct templates)
        for i in range(1, 4):
            text = _build_non_branded_prompt_variant(i, keyword.text, project, family, market)
            _validate_non_branded_safety(text, brand_name, brand_aliases, competitors)
            self._check_length(text)
            specs.append(
                GeneratedPromptSpec(
                    keyword_id=keyword.id,
                    variant_index=i,
                    prompt_type=PromptType.NON_BRANDED,
                    text=text,
                    intent=intent,
                    funnel_stage=funnel,
                    persona=persona,
                    target_country=target_country,
                    target_language=target_language,
                    commercial_intent=commercial,
                )
            )

        # Variant 4: BRANDED
        branded_text = _build_branded_prompt(keyword.text, brand_name, project, family, market)
        self._check_length(branded_text)
        specs.append(
            GeneratedPromptSpec(
                keyword_id=keyword.id,
                variant_index=4,
                prompt_type=PromptType.BRANDED,
                text=branded_text,
                intent=intent,
                funnel_stage=funnel,
                persona=persona,
                target_country=target_country,
                target_language=target_language,
                commercial_intent=commercial,
            )
        )

        # Variant 5: COMPETITOR (if competitors exist) or NON_BRANDED (buyer-oriented)
        if competitors:
            comp_text = _build_competitor_prompt(
                keyword.text, brand_name, competitors, project, family, market
            )
            self._check_length(comp_text)
            specs.append(
                GeneratedPromptSpec(
                    keyword_id=keyword.id,
                    variant_index=5,
                    prompt_type=PromptType.COMPETITOR,
                    text=comp_text,
                    intent=intent,
                    funnel_stage=funnel,
                    persona=persona,
                    target_country=target_country,
                    target_language=target_language,
                    commercial_intent=commercial,
                )
            )
        else:
            nb5_text = _build_non_branded_prompt_variant(5, keyword.text, project, family, market)
            _validate_non_branded_safety(nb5_text, brand_name, brand_aliases, competitors)
            self._check_length(nb5_text)
            specs.append(
                GeneratedPromptSpec(
                    keyword_id=keyword.id,
                    variant_index=5,
                    prompt_type=PromptType.NON_BRANDED,
                    text=nb5_text,
                    intent=intent,
                    funnel_stage=funnel,
                    persona=persona,
                    target_country=target_country,
                    target_language=target_language,
                    commercial_intent=commercial,
                )
            )

        assert len(specs) == PROMPTS_PER_KEYWORD

        # Distinctness validation: all 5 prompt texts must be unique
        # after normalized comparison.
        normalized_texts = [normalize_text_for_comparison(s.text) for s in specs]
        unique_texts = set(normalized_texts)
        if len(unique_texts) != PROMPTS_PER_KEYWORD:
            duplicates = [t for t in normalized_texts if normalized_texts.count(t) > 1]
            raise ValidationError(
                f"Generated prompts are not distinct for keyword '{keyword.text}'. "
                f"Duplicate texts: {set(duplicates)}"
            )

        return specs

    def _check_length(self, text: str) -> None:
        if len(text) > MAX_PROMPT_LENGTH:
            raise ValidationError(
                f"Generated prompt exceeds max length "
                f"({len(text)} > {MAX_PROMPT_LENGTH}): {text[:100]}..."
            )

    def specs_to_models(
        self,
        specs: list[GeneratedPromptSpec],
        prompt_set_id: uuid.UUID,
    ) -> list[Prompt]:
        """Convert GeneratedPromptSpec objects to Prompt ORM models.

        Args:
            specs: The generated prompt specifications.
            prompt_set_id: The UUID of the PromptSet these belong to.

        Returns:
            List of Prompt ORM models (not yet persisted).
        """
        prompts: list[Prompt] = []
        for spec in specs:
            prompt = Prompt(
                prompt_set_id=prompt_set_id,
                project_keyword_id=spec.keyword_id,
                variant_index=spec.variant_index,
                text=spec.text,
                prompt_type=spec.prompt_type,
                intent=spec.intent,
                funnel_stage=spec.funnel_stage,
                persona=spec.persona,
                target_country=spec.target_country,
                target_language=spec.target_language,
                commercial_intent=spec.commercial_intent,
            )
            prompts.append(prompt)
        return prompts
