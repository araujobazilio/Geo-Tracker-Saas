"""Security tests for provider adapters.

Verifies that API keys never appear in:
- repr(adapter)
- sanitized exceptions
- structured log metadata
- Authorization headers are not logged
- raw provider response is not dumped on normal parsing errors
"""

from __future__ import annotations

import logging

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.core.enums import LLMProvider, ProviderExecutionMode, ProviderSurface
from app.providers.anthropic_adapter import AnthropicProviderAdapter
from app.providers.base import ProviderRequest
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderResponseError,
)
from app.providers.google_adapter import GoogleProviderAdapter
from app.providers.http_utils import log_provider_result
from app.providers.openai_adapter import OpenAIProviderAdapter
from app.providers.perplexity_adapter import PerplexityProviderAdapter

OPENAI_KEY = "sk-openai-secret-key-12345"
ANTHROPIC_KEY = "sk-ant-secret-key-12345"
GOOGLE_KEY = "AIza-secret-key-12345"
PERPLEXITY_KEY = "pplx-secret-key-12345"


def _make_openai_settings() -> Settings:
    return Settings(
        app_env="test",
        openai_api_key=SecretStr(OPENAI_KEY),
        openai_scan_model="gpt-5.5",
    )


def _make_anthropic_settings() -> Settings:
    return Settings(
        app_env="test",
        anthropic_api_key=SecretStr(ANTHROPIC_KEY),
        anthropic_scan_model="claude-sonnet-4-5",
    )


def _make_google_settings() -> Settings:
    return Settings(
        app_env="test",
        google_api_key=SecretStr(GOOGLE_KEY),
        google_scan_model="gemini-2.5-flash",
    )


def _make_perplexity_settings() -> Settings:
    return Settings(
        app_env="test",
        perplexity_api_key=SecretStr(PERPLEXITY_KEY),
        perplexity_scan_model="sonar-pro",
    )


class TestReprDoesNotLeakKeys:
    def test_openai_repr_no_key(self) -> None:
        adapter = OpenAIProviderAdapter(settings=_make_openai_settings())
        assert OPENAI_KEY not in repr(adapter)

    def test_anthropic_repr_no_key(self) -> None:
        adapter = AnthropicProviderAdapter(settings=_make_anthropic_settings())
        assert ANTHROPIC_KEY not in repr(adapter)

    def test_google_repr_no_key(self) -> None:
        adapter = GoogleProviderAdapter(settings=_make_google_settings())
        assert GOOGLE_KEY not in repr(adapter)

    def test_perplexity_repr_no_key(self) -> None:
        adapter = PerplexityProviderAdapter(settings=_make_perplexity_settings())
        assert PERPLEXITY_KEY not in repr(adapter)


class TestExceptionsDoNotLeakKeys:
    @pytest.mark.asyncio
    async def test_openai_auth_error_no_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        adapter = OpenAIProviderAdapter(
            settings=_make_openai_settings(), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ProviderAuthenticationError) as exc_info:
            await adapter.execute(
                ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)
            )
        assert OPENAI_KEY not in str(exc_info.value)
        assert OPENAI_KEY not in repr(exc_info.value)

    @pytest.mark.asyncio
    async def test_anthropic_auth_error_no_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        adapter = AnthropicProviderAdapter(
            settings=_make_anthropic_settings(), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ProviderAuthenticationError) as exc_info:
            await adapter.execute(
                ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)
            )
        assert ANTHROPIC_KEY not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_google_auth_error_no_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        adapter = GoogleProviderAdapter(
            settings=_make_google_settings(), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ProviderAuthenticationError) as exc_info:
            await adapter.execute(
                ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)
            )
        assert GOOGLE_KEY not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_perplexity_auth_error_no_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        adapter = PerplexityProviderAdapter(
            settings=_make_perplexity_settings(), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ProviderAuthenticationError) as exc_info:
            await adapter.execute(
                ProviderRequest(prompt="test", mode=ProviderExecutionMode.WEB_GROUNDED)
            )
        assert PERPLEXITY_KEY not in str(exc_info.value)


class TestLogsDoNotLeakKeys:
    def test_log_provider_result_no_key(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="app.providers")
        log_provider_result(
            provider=LLMProvider.OPENAI,
            surface=ProviderSurface.OPENAI_RESPONSES_API.value,
            execution_mode=ProviderExecutionMode.MODEL_ONLY.value,
            requested_model="gpt-5.5",
            returned_model="gpt-5.5",
            provider_request_id="resp_123",
            status="ok",
            latency_ms=100,
            usage_input_tokens=10,
            usage_output_tokens=5,
            search_requests=None,
        )
        for record in caplog.records:
            msg = record.getMessage()
            assert "sk-" not in msg
            assert "key" not in msg.lower()
            assert "authorization" not in msg.lower()
            assert "bearer" not in msg.lower()

    @pytest.mark.asyncio
    async def test_openai_success_log_no_key(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.INFO, logger="app.providers")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "resp_1",
                    "model": "gpt-5.5",
                    "output_text": "Hello",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            )

        adapter = OpenAIProviderAdapter(
            settings=_make_openai_settings(), transport=httpx.MockTransport(handler)
        )
        await adapter.execute(ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY))
        for record in caplog.records:
            msg = record.getMessage()
            assert OPENAI_KEY not in msg
            assert "authorization" not in msg.lower()
            assert "bearer" not in msg.lower()


class TestNoRawResponseLeakage:
    @pytest.mark.asyncio
    async def test_openai_malformed_response_no_body_dump(self) -> None:
        """On a parsing error, the raw response body must not be dumped
        into the exception message.
        """
        raw_body = {"some": "unexpected", "data": "structure"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=raw_body)

        adapter = OpenAIProviderAdapter(
            settings=_make_openai_settings(), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ProviderResponseError) as exc_info:
            await adapter.execute(
                ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)
            )
        error_msg = str(exc_info.value)
        # The raw response body should NOT be in the error message.
        assert "unexpected" not in error_msg
        assert "data" not in error_msg

    @pytest.mark.asyncio
    async def test_google_malformed_response_no_body_dump(self) -> None:
        raw_body = {"unexpected": "garbage_value_123"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=raw_body)

        adapter = GoogleProviderAdapter(
            settings=_make_google_settings(), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(ProviderResponseError) as exc_info:
            await adapter.execute(
                ProviderRequest(prompt="test", mode=ProviderExecutionMode.MODEL_ONLY)
            )
        error_msg = str(exc_info.value)
        assert "garbage_value_123" not in error_msg


class TestPromptTextNotLogged:
    @pytest.mark.asyncio
    async def test_openai_prompt_not_in_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """Prompt text should not appear in structured logs."""
        caplog.set_level(logging.INFO, logger="app.providers")
        secret_prompt = "ThisIsASecretPromptForTesting123"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "resp_1",
                    "model": "gpt-5.5",
                    "output_text": "Hello",
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            )

        adapter = OpenAIProviderAdapter(
            settings=_make_openai_settings(), transport=httpx.MockTransport(handler)
        )
        await adapter.execute(
            ProviderRequest(prompt=secret_prompt, mode=ProviderExecutionMode.MODEL_ONLY)
        )
        for record in caplog.records:
            msg = record.getMessage()
            assert secret_prompt not in msg
