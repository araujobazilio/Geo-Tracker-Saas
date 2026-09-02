"""Form helpers for the web layer.

Handles parsing of multi-step onboarding wizard form data from a single
POST submission (progressive sections in the browser, one final submit).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import LLMProvider


@dataclass
class OnboardingFormData:
    """Parsed onboarding wizard form data, ready for OnboardingRequest."""

    name: str = ""
    domain: str = ""
    brand_name: str = ""
    brand_aliases: list[str] = field(default_factory=list)
    industry: str = ""
    target_country: str = ""
    target_language: str = ""
    target_audience: str = ""
    keywords: list[dict[str, str]] = field(default_factory=list)
    competitors: list[dict[str, Any]] = field(default_factory=list)
    providers: list[LLMProvider] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


def parse_onboarding_form(form: dict[str, Any]) -> OnboardingFormData:
    """Parse the wizard form submission into structured data.

    The browser sends all steps in one POST. Keywords and competitors
    are sent as repeated form fields or JSON arrays.
    """
    data = OnboardingFormData()
    data.name = (form.get("name") or "").strip()
    data.domain = (form.get("domain") or "").strip()
    data.brand_name = (form.get("brand_name") or "").strip()
    data.brand_aliases = [
        a.strip() for a in (form.get("brand_aliases") or "").split(",") if a.strip()
    ]
    data.industry = (form.get("industry") or "").strip()
    data.target_country = (form.get("target_country") or "").strip()
    data.target_language = (form.get("target_language") or "").strip()
    data.target_audience = (form.get("target_audience") or "").strip()

    # Keywords: sent as JSON array or repeated fields
    raw_keywords = form.get("keywords")
    if isinstance(raw_keywords, str):
        import json

        try:
            raw_keywords = json.loads(raw_keywords)
        except (json.JSONDecodeError, TypeError):
            raw_keywords = []
    if isinstance(raw_keywords, list):
        for kw in raw_keywords:
            if isinstance(kw, dict):
                data.keywords.append(
                    {
                        "text": (kw.get("text") or "").strip(),
                        "intent": (kw.get("intent") or "").strip() or "",
                        "funnel_stage": (kw.get("funnel_stage") or "").strip() or "",
                    }
                )
            elif isinstance(kw, str):
                data.keywords.append({"text": kw.strip(), "intent": "", "funnel_stage": ""})

    # Competitors
    raw_competitors = form.get("competitors")
    if isinstance(raw_competitors, str):
        import json

        try:
            raw_competitors = json.loads(raw_competitors)
        except (json.JSONDecodeError, TypeError):
            raw_competitors = []
    if isinstance(raw_competitors, list):
        for comp in raw_competitors:
            if isinstance(comp, dict):
                aliases = comp.get("aliases") or []
                if isinstance(aliases, str):
                    aliases = [a.strip() for a in aliases.split(",") if a.strip()]
                data.competitors.append(
                    {
                        "name": (comp.get("name") or "").strip(),
                        "domain": (comp.get("domain") or "").strip(),
                        "aliases": aliases,
                    }
                )

    # Providers
    raw_providers = form.get("providers")
    if isinstance(raw_providers, str):
        try:
            import json

            raw_providers = json.loads(raw_providers)
        except (json.JSONDecodeError, TypeError):
            raw_providers = [raw_providers]
    if isinstance(raw_providers, list):
        for p in raw_providers:
            with contextlib.suppress(ValueError):
                data.providers.append(LLMProvider(p))
    elif isinstance(raw_providers, str):
        with contextlib.suppress(ValueError):
            data.providers.append(LLMProvider(raw_providers))

    # Validation
    if not data.name:
        data.errors["name"] = "Project name is required."
    if not data.domain:
        data.errors["domain"] = "Domain is required."
    if not data.brand_name:
        data.errors["brand_name"] = "Brand name is required."
    if not data.keywords:
        data.errors["keywords"] = "At least one topic is required."

    return data
