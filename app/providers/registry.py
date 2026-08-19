"""Provider registry — constructs adapters on demand and exposes capabilities.

The registry does NOT instantiate all providers eagerly at application
startup. A missing Anthropic key must not prevent the application from
starting if Anthropic is not being used.

Health endpoints remain healthy when provider credentials are missing.

capabilities() is available WITHOUT credentials — it returns static
adapter facts and makes ZERO network calls.
"""

from __future__ import annotations

from app.core.enums import LLMProvider
from app.core.logging import get_logger
from app.providers.base import ProviderAdapter, ProviderCapabilities
from app.providers.errors import ProviderConfigurationError

logger = get_logger("app.providers.registry")

# Static capabilities per provider — queryable without credentials.
_STATIC_CAPABILITIES: dict[LLMProvider, ProviderCapabilities] = {
    LLMProvider.OPENAI: ProviderCapabilities(
        supports_model_only=True,
        supports_web_grounded=True,
        supports_citations=True,
        supports_search_result_metadata=True,
    ),
    LLMProvider.ANTHROPIC: ProviderCapabilities(
        supports_model_only=True,
        supports_web_grounded=True,
        supports_citations=True,
        supports_search_result_metadata=True,
    ),
    LLMProvider.GOOGLE: ProviderCapabilities(
        supports_model_only=True,
        supports_web_grounded=False,
        supports_citations=False,
        supports_search_result_metadata=False,
    ),
    LLMProvider.PERPLEXITY: ProviderCapabilities(
        supports_model_only=False,
        supports_web_grounded=True,
        supports_citations=True,
        supports_search_result_metadata=True,
    ),
}


class ProviderRegistry:
    """Registry for provider adapters.

    Adapters are constructed lazily — only when get() is called.
    This means a missing API key for one provider does not prevent
    the application from starting or other providers from working.
    """

    def __init__(self) -> None:
        self._instances: dict[LLMProvider, ProviderAdapter] = {}

    def get(self, provider: LLMProvider) -> ProviderAdapter:
        """Get or construct a provider adapter.

        Validates that the provider has the minimum required configuration
        (API key and scan model) before returning the adapter.

        Raises:
            ProviderConfigurationError: if the provider cannot be
                constructed because required configuration is missing.
        """
        if provider in self._instances:
            return self._instances[provider]

        # Validate configuration before constructing.
        self._validate_configuration(provider)

        adapter = self._construct(provider)
        self._instances[provider] = adapter
        return adapter

    def capabilities(self, provider: LLMProvider) -> ProviderCapabilities:
        """Return capabilities for a provider WITHOUT performing any API call
        and WITHOUT requiring credentials.

        Capabilities are static adapter facts (e.g. "Google supports
        MODEL_ONLY but not WEB_GROUNDED"). They do not depend on whether
        an API key is configured.

        Raises:
            ProviderConfigurationError: if the provider is unknown.
        """
        caps = _STATIC_CAPABILITIES.get(provider)
        if caps is None:
            raise ProviderConfigurationError(
                f"Unknown provider: {provider}",
                provider=provider.value,
            )
        return caps

    def is_configured(self, provider: LLMProvider) -> bool:
        """Check if a provider has the minimum required configuration.

        This does NOT construct the adapter — it only checks settings.
        Returns True if both API key and scan model are non-empty.
        """
        from app.config import get_settings

        settings = get_settings()
        if provider == LLMProvider.OPENAI:
            return bool(settings.openai_api_key.get_secret_value() and settings.openai_scan_model)
        if provider == LLMProvider.ANTHROPIC:
            return bool(
                settings.anthropic_api_key.get_secret_value() and settings.anthropic_scan_model
            )
        if provider == LLMProvider.GOOGLE:
            return bool(settings.google_api_key.get_secret_value() and settings.google_scan_model)
        if provider == LLMProvider.PERPLEXITY:
            return bool(
                settings.perplexity_api_key.get_secret_value() and settings.perplexity_scan_model
            )
        return False

    def _validate_configuration(self, provider: LLMProvider) -> None:
        """Validate that the provider has required configuration.

        Raises ProviderConfigurationError if API key or scan model is empty.
        """
        from app.config import get_settings

        settings = get_settings()
        provider_str = provider.value

        if provider == LLMProvider.OPENAI:
            if not settings.openai_api_key.get_secret_value():
                raise ProviderConfigurationError(
                    "OpenAI API key is not configured.",
                    provider=provider_str,
                )
            if not settings.openai_scan_model:
                raise ProviderConfigurationError(
                    "OpenAI scan model is not configured.",
                    provider=provider_str,
                )
        elif provider == LLMProvider.ANTHROPIC:
            if not settings.anthropic_api_key.get_secret_value():
                raise ProviderConfigurationError(
                    "Anthropic API key is not configured.",
                    provider=provider_str,
                )
            if not settings.anthropic_scan_model:
                raise ProviderConfigurationError(
                    "Anthropic scan model is not configured.",
                    provider=provider_str,
                )
        elif provider == LLMProvider.GOOGLE:
            if not settings.google_api_key.get_secret_value():
                raise ProviderConfigurationError(
                    "Google API key is not configured.",
                    provider=provider_str,
                )
            if not settings.google_scan_model:
                raise ProviderConfigurationError(
                    "Google scan model is not configured.",
                    provider=provider_str,
                )
        elif provider == LLMProvider.PERPLEXITY:
            if not settings.perplexity_api_key.get_secret_value():
                raise ProviderConfigurationError(
                    "Perplexity API key is not configured.",
                    provider=provider_str,
                )
            if not settings.perplexity_scan_model:
                raise ProviderConfigurationError(
                    "Perplexity scan model is not configured.",
                    provider=provider_str,
                )

    def _construct(self, provider: LLMProvider) -> ProviderAdapter:
        """Construct a provider adapter lazily."""
        if provider == LLMProvider.OPENAI:
            from app.providers.openai_adapter import OpenAIProviderAdapter

            return OpenAIProviderAdapter()

        if provider == LLMProvider.ANTHROPIC:
            from app.providers.anthropic_adapter import AnthropicProviderAdapter

            return AnthropicProviderAdapter()

        if provider == LLMProvider.GOOGLE:
            from app.providers.google_adapter import GoogleProviderAdapter

            return GoogleProviderAdapter()

        if provider == LLMProvider.PERPLEXITY:
            from app.providers.perplexity_adapter import PerplexityProviderAdapter

            return PerplexityProviderAdapter()

        raise ProviderConfigurationError(
            f"Unknown provider: {provider}",
            provider=provider.value,
        )
