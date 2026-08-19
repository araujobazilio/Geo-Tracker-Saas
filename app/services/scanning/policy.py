"""Fixed Phase 6 STANDARD scan provider methodology."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.core.enums import LLMProvider, ProviderExecutionMode, ProviderSurface

PROVIDER_ORDER = (
    LLMProvider.OPENAI,
    LLMProvider.ANTHROPIC,
    LLMProvider.GOOGLE,
    LLMProvider.PERPLEXITY,
)


@dataclass(frozen=True)
class ProviderExecutionTarget:
    provider: LLMProvider
    surface: ProviderSurface
    mode: ProviderExecutionMode
    requested_model: str


class ProviderExecutionPolicy:
    """Derive immutable STANDARD scan targets; public APIs cannot override them."""

    def target(self, provider: LLMProvider, settings: Settings) -> ProviderExecutionTarget:
        if provider == LLMProvider.OPENAI:
            return ProviderExecutionTarget(
                provider,
                ProviderSurface.OPENAI_RESPONSES_API,
                ProviderExecutionMode.WEB_GROUNDED,
                settings.openai_scan_model,
            )
        if provider == LLMProvider.ANTHROPIC:
            return ProviderExecutionTarget(
                provider,
                ProviderSurface.ANTHROPIC_MESSAGES_API,
                ProviderExecutionMode.WEB_GROUNDED,
                settings.anthropic_scan_model,
            )
        if provider == LLMProvider.GOOGLE:
            return ProviderExecutionTarget(
                provider,
                ProviderSurface.GOOGLE_INTERACTIONS_API,
                ProviderExecutionMode.MODEL_ONLY,
                settings.google_scan_model,
            )
        if provider == LLMProvider.PERPLEXITY:
            return ProviderExecutionTarget(
                provider,
                ProviderSurface.PERPLEXITY_SONAR_API,
                ProviderExecutionMode.WEB_GROUNDED,
                settings.perplexity_scan_model,
            )
        raise ValueError(f"Unsupported provider: {provider.value}")
