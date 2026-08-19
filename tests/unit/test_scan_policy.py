"""Fast unit coverage for the fixed Phase 6 scan policy and error mapping."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.core.enums import (
    LLMProvider,
    ProviderErrorCode,
    ProviderExecutionMode,
    ProviderSurface,
)
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderBadRequestError,
    ProviderConfigurationError,
    ProviderError,
    ProviderModeNotAllowedError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderSearchError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.services.scanning.errors import map_provider_error, safe_error_message
from app.services.scanning.policy import PROVIDER_ORDER, ProviderExecutionPolicy

ERROR_CODE_CASES = (
    (ProviderConfigurationError, ProviderErrorCode.CONFIGURATION_ERROR),
    (ProviderAuthenticationError, ProviderErrorCode.AUTHENTICATION_ERROR),
    (ProviderRateLimitError, ProviderErrorCode.RATE_LIMITED),
    (ProviderTimeoutError, ProviderErrorCode.TIMEOUT),
    (ProviderUnavailableError, ProviderErrorCode.PROVIDER_UNAVAILABLE),
    (ProviderBadRequestError, ProviderErrorCode.INVALID_REQUEST),
    (ProviderResponseError, ProviderErrorCode.MALFORMED_RESPONSE),
    (ProviderSearchError, ProviderErrorCode.SEARCH_ERROR),
    (ProviderModeNotAllowedError, ProviderErrorCode.MODE_NOT_ALLOWED),
)


def test_provider_execution_policy_has_exact_order_and_mapping() -> None:
    settings = Settings(
        app_env="test",
        openai_scan_model="openai-model",
        anthropic_scan_model="anthropic-model",
        google_scan_model="google-model",
        perplexity_scan_model="perplexity-model",
    )
    policy = ProviderExecutionPolicy()

    assert PROVIDER_ORDER == (
        LLMProvider.OPENAI,
        LLMProvider.ANTHROPIC,
        LLMProvider.GOOGLE,
        LLMProvider.PERPLEXITY,
    )
    assert tuple(
        (
            target.provider,
            target.surface,
            target.mode,
            target.requested_model,
        )
        for target in (policy.target(provider, settings) for provider in PROVIDER_ORDER)
    ) == (
        (
            LLMProvider.OPENAI,
            ProviderSurface.OPENAI_RESPONSES_API,
            ProviderExecutionMode.WEB_GROUNDED,
            "openai-model",
        ),
        (
            LLMProvider.ANTHROPIC,
            ProviderSurface.ANTHROPIC_MESSAGES_API,
            ProviderExecutionMode.WEB_GROUNDED,
            "anthropic-model",
        ),
        (
            LLMProvider.GOOGLE,
            ProviderSurface.GOOGLE_INTERACTIONS_API,
            ProviderExecutionMode.MODEL_ONLY,
            "google-model",
        ),
        (
            LLMProvider.PERPLEXITY,
            ProviderSurface.PERPLEXITY_SONAR_API,
            ProviderExecutionMode.WEB_GROUNDED,
            "perplexity-model",
        ),
    )


@pytest.mark.parametrize(("error_type", "expected_code"), ERROR_CODE_CASES)
def test_map_provider_error_is_deterministic_for_every_subclass(
    error_type: type[ProviderError], expected_code: ProviderErrorCode
) -> None:
    assert set(ProviderError.__subclasses__()) == {
        case_error_type for case_error_type, _ in ERROR_CODE_CASES
    }
    error = error_type("sanitized provider failure", provider="test-provider")

    assert map_provider_error(error) is expected_code
    assert map_provider_error(error) is expected_code


def test_unmapped_provider_error_uses_internal_error_code() -> None:
    assert (
        map_provider_error(ProviderError("generic provider failure"))
        is ProviderErrorCode.INTERNAL_ERROR
    )


def test_safe_error_message_for_unknown_exception_is_constant_and_sanitized() -> None:
    secret = "must-not-leak"

    message = safe_error_message(RuntimeError(secret))

    assert message == "Internal scan execution failure."
    assert secret not in message


def test_settings_accept_positive_phase_6_values() -> None:
    settings = Settings(
        app_env="test",
        scan_max_concurrency=2,
        scan_reservation_ttl_seconds=3,
        scan_stale_after_seconds=4,
        openai_web_search_max_tool_calls=5,
        anthropic_web_search_max_uses=6,
        provider_connect_timeout_seconds=0.5,
        provider_read_timeout_seconds=1.5,
        provider_max_output_tokens=7,
    )

    assert settings.scan_max_concurrency == 2
    assert settings.scan_reservation_ttl_seconds == 3
    assert settings.scan_stale_after_seconds == 4
    assert settings.openai_web_search_max_tool_calls == 5
    assert settings.anthropic_web_search_max_uses == 6
    assert settings.provider_connect_timeout_seconds == 0.5
    assert settings.provider_read_timeout_seconds == 1.5
    assert settings.provider_max_output_tokens == 7
