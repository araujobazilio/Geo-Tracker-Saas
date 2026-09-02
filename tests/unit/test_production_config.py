"""Production configuration validation tests.

Tests fail-fast validation for deployment-critical configuration.
No network calls are made.
"""

from __future__ import annotations

import os

import pytest


def _clear_settings_cache() -> None:
    """Clear the lru_cache on get_settings so new Settings() is created."""
    from app.config import get_settings

    get_settings.cache_clear()


@pytest.fixture()
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Remove all app-related env vars so each test starts clean."""
    for key in list(os.environ.keys()):
        if key.startswith(
            (
                "APP_",
                "DATABASE_",
                "REDIS_",
                "DEV_SEED",
                "EMAIL_",
                "SMTP_",
                "ALLOWED_HOSTS",
                "REGISTRATION_",
                "CORS_",
            )
        ):
            monkeypatch.delenv(key, raising=False)
    _clear_settings_cache()
    yield
    _clear_settings_cache()


class TestProductionConfigValidation:
    """Test production fail-fast validation."""

    def test_strong_secret_accepted(self, _clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
        """A strong secret key is accepted in production."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("DEV_SEED_ENABLED", "false")

        from app.config import Settings

        settings = Settings()
        assert settings.is_production
        assert settings.session_cookie_secure is True

    def test_weak_secret_rejected(self, _clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
        """A placeholder secret key is rejected in production."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "change-me")
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")

        from app.config import Settings

        with pytest.raises(ValueError, match="insecure placeholder"):
            Settings()

    def test_empty_secret_rejected(self, _clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty secret key is rejected in production."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "")
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")

        from app.config import Settings

        with pytest.raises(ValueError, match="APP_SECRET_KEY must be set"):
            Settings()

    def test_short_secret_rejected(self, _clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
        """A too-short secret key is rejected in production."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "shortkey")
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")

        from app.config import Settings

        with pytest.raises(ValueError, match="at least 32"):
            Settings()

    def test_secure_cookie_enforced_in_production(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Session cookie Secure flag is enforced True in production."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")

        from app.config import Settings

        settings = Settings()
        assert settings.session_cookie_secure is True

    def test_dev_seed_rejected_in_production(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEV_SEED_ENABLED=true is rejected in production."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("DEV_SEED_ENABLED", "true")

        from app.config import Settings

        with pytest.raises(ValueError, match="DEV_SEED_ENABLED"):
            Settings()

    def test_non_https_public_base_url_rejected(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-HTTPS APP_PUBLIC_BASE_URL is rejected in production."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "http://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")

        from app.config import Settings

        with pytest.raises(ValueError, match="HTTPS"):
            Settings()

    def test_dev_database_password_rejected(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DATABASE_URL with dev password placeholder is rejected in production."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv(
            "DATABASE_URL",
            "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@db:5432/geo",
        )
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")

        from app.config import Settings

        with pytest.raises(ValueError, match="development password"):
            Settings()

    def test_empty_redis_url_rejected(self, _clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty REDIS_URL is rejected in production."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")

        from app.config import Settings

        with pytest.raises(ValueError, match="REDIS_URL"):
            Settings()

    def test_missing_allowed_hosts_rejected(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing ALLOWED_HOSTS is rejected in production."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.delenv("ALLOWED_HOSTS", raising=False)

        from app.config import Settings

        with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
            Settings()

    def test_email_enabled_missing_smtp_rejected(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """EMAIL_ENABLED=true with missing SMTP_HOST is rejected."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("EMAIL_ENABLED", "true")
        monkeypatch.setenv("SMTP_HOST", "")

        from app.config import Settings

        with pytest.raises(ValueError, match="SMTP_HOST"):
            Settings()

    def test_email_disabled_empty_smtp_accepted(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """EMAIL_ENABLED=false with empty SMTP settings is accepted."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("EMAIL_ENABLED", "false")
        monkeypatch.setenv("SMTP_HOST", "")

        from app.config import Settings

        settings = Settings()
        assert settings.email_enabled is False

    def test_development_allows_weak_secret(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Development mode allows placeholder secrets."""
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("APP_SECRET_KEY", "change-me")

        from app.config import Settings

        settings = Settings()
        assert settings.app_env == "development"

    def test_registration_mode_closed(self, _clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
        """REGISTRATION_MODE=closed is properly parsed."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("REGISTRATION_MODE", "closed")

        from app.config import Settings

        settings = Settings()
        assert settings.is_registration_closed is True

    def test_registration_mode_open(self, _clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
        """REGISTRATION_MODE=open is properly parsed."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("REGISTRATION_MODE", "open")

        from app.config import Settings

        settings = Settings()
        assert settings.is_registration_closed is False

    def test_production_omitted_registration_defaults_closed(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Production + REGISTRATION_MODE omitted => closed (fail-closed)."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        # REGISTRATION_MODE is NOT set.

        from app.config import Settings

        settings = Settings()
        assert settings.registration_mode == "closed"
        assert settings.is_registration_closed is True

    def test_development_omitted_registration_defaults_open(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Development + REGISTRATION_MODE omitted => open."""
        monkeypatch.setenv("APP_ENV", "development")
        # REGISTRATION_MODE is NOT set.

        from app.config import Settings

        settings = Settings()
        assert settings.registration_mode == "open"
        assert settings.is_registration_closed is False

    def test_test_omitted_registration_defaults_open(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test + REGISTRATION_MODE omitted => open."""
        monkeypatch.setenv("APP_ENV", "test")
        # REGISTRATION_MODE is NOT set.

        from app.config import Settings

        settings = Settings()
        assert settings.registration_mode == "open"
        assert settings.is_registration_closed is False

    def test_production_explicit_open_still_works(
        self, _clean_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Production + explicit REGISTRATION_MODE=open => open (not silently closed)."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("REGISTRATION_MODE", "open")

        from app.config import Settings

        settings = Settings()
        assert settings.registration_mode == "open"
        assert settings.is_registration_closed is False

    def test_build_metadata(self, _clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
        """Build metadata is exposed safely."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
        monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://example.com")
        monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
        monkeypatch.setenv("APP_VERSION", "1.2.3")
        monkeypatch.setenv("GIT_SHA", "abc123")
        monkeypatch.setenv("BUILD_TIME", "2026-01-01T00:00:00Z")

        from app.config import Settings

        settings = Settings()
        meta = settings.build_metadata
        assert meta["version"] == "1.2.3"
        assert meta["git_sha"] == "abc123"
        assert meta["build_time"] == "2026-01-01T00:00:00Z"
