"""Unit tests for configuration loading."""

from __future__ import annotations

from app.config import Settings


def test_default_settings() -> None:
    settings = Settings()
    assert settings.app_env in {"development", "staging", "production", "test"}
    assert settings.app_name == "GEO Tracker"
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.redis_url.startswith("redis://")


def test_cors_origin_list_parses_comma_separated() -> None:
    settings = Settings(cors_origins="https://a.com, https://b.com ,")
    assert settings.cors_origin_list == ["https://a.com", "https://b.com"]


def test_log_level_normalized_to_upper() -> None:
    settings = Settings(log_level="debug")
    assert settings.log_level == "DEBUG"
