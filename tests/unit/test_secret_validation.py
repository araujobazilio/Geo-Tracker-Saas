"""Unit tests for production secret validation (APP_SECRET_KEY fail-fast)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

# Valid production-grade config values for tests that need successful construction.
# All fields that have production validation must be explicitly set to avoid
# env var pollution from integration tests that set os.environ directly.
_PROD_DB_URL = "postgresql+psycopg://user:strongpass@db:5432/geo"
_PROD_REDIS_URL = "redis://redis:6379/0"
_PROD_BASE_URL = "https://example.com"
_PROD_ALLOWED_HOSTS = "example.com"


def _prod_kwargs(**overrides: object) -> dict[str, object]:
    """Return kwargs for a valid production Settings, with optional overrides."""
    base: dict[str, object] = {
        "app_env": "production",
        "database_url": _PROD_DB_URL,
        "redis_url": _PROD_REDIS_URL,
        "app_public_base_url": _PROD_BASE_URL,
        "allowed_hosts": _PROD_ALLOWED_HOSTS,
        "email_enabled": False,
        "dev_seed_enabled": False,
    }
    base.update(overrides)
    return base


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
        Settings(**_prod_kwargs(app_secret_key="change-me"))
    assert "insecure placeholder" in str(exc.value).lower()


def test_production_empty_secret_rejected() -> None:
    """In production, an empty secret must be rejected."""
    with pytest.raises(ValidationError) as exc:
        Settings(**_prod_kwargs(app_secret_key=""))
    assert "empty" in str(exc.value).lower()


def test_production_short_secret_rejected() -> None:
    """In production, a secret shorter than 32 chars must be rejected."""
    with pytest.raises(ValidationError) as exc:
        Settings(**_prod_kwargs(app_secret_key="short-secret-15chars"))
    assert "32" in str(exc.value)


def test_production_strong_secret_accepted() -> None:
    """In production, a strong (>=32 char, non-placeholder) secret is accepted."""
    strong = "x" * 48
    settings = Settings(**_prod_kwargs(app_secret_key=strong))
    assert settings.app_secret_key.get_secret_value() == strong


def test_staging_placeholder_rejected() -> None:
    """Staging must enforce the same rules as production."""
    with pytest.raises(ValidationError):
        Settings(
            **_prod_kwargs(
                app_env="staging",
                app_secret_key="change-me-to-a-long-random-string",
            ),
        )


def test_staging_strong_secret_accepted() -> None:
    strong = "a-very-strong-staging-secret-key-123456"
    settings = Settings(
        **_prod_kwargs(
            app_env="staging",
            app_secret_key=strong,
        ),
    )
    assert settings.is_staging is True


def test_error_message_does_not_leak_secret() -> None:
    """The validation error must never include the real secret value."""
    secret = "super-secret-value-that-should-not-leak-123"
    with pytest.raises(ValidationError) as exc:
        Settings(**_prod_kwargs(app_secret_key=secret[:10]))
    assert secret not in str(exc.value)
