"""Tests for ProviderRegistry."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.core.enums import LLMProvider, ProviderSurface
from app.providers.base import ProviderCapabilities
from app.providers.errors import ProviderConfigurationError
from app.providers.registry import ProviderRegistry


def _make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "app_env": "test",
        "openai_api_key": SecretStr("sk-openai-key"),
        "openai_scan_model": "gpt-5.5",
        "anthropic_api_key": SecretStr("sk-ant-key"),
        "anthropic_scan_model": "claude-sonnet-4-5",
        "google_api_key": SecretStr("AIza-google-key"),
        "google_scan_model": "gemini-2.5-flash",
        "perplexity_api_key": SecretStr("pplx-key"),
        "perplexity_scan_model": "sonar-pro",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@contextmanager
def patch_settings(settings: Settings) -> Generator[None, None, None]:
    """Temporarily replace get_settings() return value.

    Patches get_settings in app.config AND in each adapter module, since
    they import get_settings at module load time.
    """
    with (
        patch("app.config.get_settings", return_value=settings),
        patch("app.providers.openai_adapter.get_settings", return_value=settings),
        patch("app.providers.anthropic_adapter.get_settings", return_value=settings),
        patch("app.providers.google_adapter.get_settings", return_value=settings),
        patch("app.providers.perplexity_adapter.get_settings", return_value=settings),
        patch("app.providers.http_utils.get_settings", return_value=settings),
    ):
        yield


class TestProviderRegistry:
    def test_get_openai_returns_adapter(self) -> None:
        with patch_settings(_make_settings()):
            registry = ProviderRegistry()
            adapter = registry.get(LLMProvider.OPENAI)
            assert adapter.provider == LLMProvider.OPENAI
            assert adapter.surface == ProviderSurface.OPENAI_RESPONSES_API

    def test_missing_openai_key_raises_configuration_error(self) -> None:
        with patch_settings(_make_settings(openai_api_key=SecretStr(""))):
            registry = ProviderRegistry()
            with pytest.raises(ProviderConfigurationError):
                registry.get(LLMProvider.OPENAI)

    def test_missing_model_raises_configuration_error(self) -> None:
        with patch_settings(_make_settings(anthropic_scan_model="")):
            registry = ProviderRegistry()
            with pytest.raises(ProviderConfigurationError):
                registry.get(LLMProvider.ANTHROPIC)

    def test_one_missing_provider_does_not_break_another(self) -> None:
        """Missing Anthropic config must not prevent OpenAI from working."""
        with patch_settings(
            _make_settings(anthropic_api_key=SecretStr(""), anthropic_scan_model="")
        ):
            registry = ProviderRegistry()
            # OpenAI should work fine.
            openai_adapter = registry.get(LLMProvider.OPENAI)
            assert openai_adapter.provider == LLMProvider.OPENAI
            # Anthropic should fail.
            with pytest.raises(ProviderConfigurationError):
                registry.get(LLMProvider.ANTHROPIC)

    def test_capabilities_without_external_request(self) -> None:
        """capabilities() must NOT perform any API call."""
        with patch_settings(_make_settings()):
            registry = ProviderRegistry()
            caps = registry.capabilities(LLMProvider.OPENAI)
            assert isinstance(caps, ProviderCapabilities)
            assert caps.supports_model_only is True
            assert caps.supports_web_grounded is True

    def test_capabilities_without_credentials(self) -> None:
        """capabilities() must work WITHOUT any credentials configured.
        Capabilities are static adapter facts, not runtime configuration.
        """
        # All keys and models empty.
        empty_settings = _make_settings(
            openai_api_key=SecretStr(""),
            openai_scan_model="",
            anthropic_api_key=SecretStr(""),
            anthropic_scan_model="",
            google_api_key=SecretStr(""),
            google_scan_model="",
            perplexity_api_key=SecretStr(""),
            perplexity_scan_model="",
        )
        with patch_settings(empty_settings):
            registry = ProviderRegistry()
            # capabilities() should work even with no credentials.
            caps = registry.capabilities(LLMProvider.OPENAI)
            assert caps.supports_model_only is True
            assert caps.supports_web_grounded is True

            caps = registry.capabilities(LLMProvider.GOOGLE)
            assert caps.supports_web_grounded is False

            caps = registry.capabilities(LLMProvider.PERPLEXITY)
            assert caps.supports_model_only is False

    def test_google_web_grounded_false(self) -> None:
        with patch_settings(_make_settings()):
            registry = ProviderRegistry()
            caps = registry.capabilities(LLMProvider.GOOGLE)
            assert caps.supports_web_grounded is False

    def test_perplexity_model_only_false(self) -> None:
        with patch_settings(_make_settings()):
            registry = ProviderRegistry()
            caps = registry.capabilities(LLMProvider.PERPLEXITY)
            assert caps.supports_model_only is False

    def test_is_configured_true_when_key_and_model_present(self) -> None:
        with patch_settings(_make_settings()):
            registry = ProviderRegistry()
            assert registry.is_configured(LLMProvider.OPENAI) is True

    def test_is_configured_false_when_key_missing(self) -> None:
        with patch_settings(_make_settings(openai_api_key=SecretStr(""))):
            registry = ProviderRegistry()
            assert registry.is_configured(LLMProvider.OPENAI) is False

    def test_is_configured_false_when_model_missing(self) -> None:
        with patch_settings(_make_settings(google_scan_model="")):
            registry = ProviderRegistry()
            assert registry.is_configured(LLMProvider.GOOGLE) is False

    def test_registry_caches_adapter_instances(self) -> None:
        """Getting the same provider twice returns the same instance."""
        with patch_settings(_make_settings()):
            registry = ProviderRegistry()
            adapter1 = registry.get(LLMProvider.OPENAI)
            adapter2 = registry.get(LLMProvider.OPENAI)
            assert adapter1 is adapter2
