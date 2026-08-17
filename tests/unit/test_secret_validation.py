"""Unit tests for production secret validation (APP_SECRET_KEY fail-fast)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_development_placeholder_allowed() -> None:
    """In development, the default placeholder is accepted."""
    settings = Settings(app_env="development", app_secret_key="change-me")
    assert settings.app_secret_key.get_secret_value() == "change-me"


def test_test_env_placeholder_allowed() -> None:
    """In test, the default placeholder is accepted."""
    settings = Settings(app_env="test", app_secret_key="change-me")
    assert settings.app_env == "test"


def test_production_placeholder_rejected() -> None:
    """In production, the known placeholder must be rejected."""
    with pytest.raises(ValidationError) as exc:
        Settings(app_env="production", app_secret_key="change-me")
    assert "insecure placeholder" in str(exc.value).lower()


def test_production_empty_secret_rejected() -> None:
    """In production, an empty secret must be rejected."""
    with pytest.raises(ValidationError) as exc:
        Settings(app_env="production", app_secret_key="")
    assert "empty" in str(exc.value).lower()


def test_production_short_secret_rejected() -> None:
    """In production, a secret shorter than 32 chars must be rejected."""
    with pytest.raises(ValidationError) as exc:
        Settings(app_env="production", app_secret_key="short-secret-15chars")
    assert "32" in str(exc.value)


def test_production_strong_secret_accepted() -> None:
    """In production, a strong (>=32 char, non-placeholder) secret is accepted."""
    strong = "x" * 48
    settings = Settings(app_env="production", app_secret_key=strong)
    assert settings.app_secret_key.get_secret_value() == strong


def test_staging_placeholder_rejected() -> None:
    """Staging must enforce the same rules as production."""
    with pytest.raises(ValidationError):
        Settings(app_env="staging", app_secret_key="change-me-to-a-long-random-string")


def test_staging_strong_secret_accepted() -> None:
    strong = "a-very-strong-staging-secret-key-123456"
    settings = Settings(app_env="staging", app_secret_key=strong)
    assert settings.is_staging is True


def test_error_message_does_not_leak_secret() -> None:
    """The validation error must never include the real secret value."""
    secret = "super-secret-value-that-should-not-leak-123"
    with pytest.raises(ValidationError) as exc:
        Settings(app_env="production", app_secret_key=secret[:10])
    assert secret not in str(exc.value)
