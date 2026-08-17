"""GEO Tracker — application configuration.

All configuration is environment-driven via pydantic-settings.
Secrets must NEVER be hardcoded here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    app_env: Literal["development", "staging", "production", "test"] = "development"
    app_name: str = "GEO Tracker"
    app_secret_key: SecretStr = SecretStr("change-me")
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # --- Logging ---
    log_level: str = "INFO"
    log_json: bool = False

    # --- Database ---
    database_url: str = Field(
        default="postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:5432/geo_tracker"
    )
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Security ---
    cors_origins: str = ""

    # --- AI providers (placeholders, not validated in Phase 0/1) ---
    openai_api_key: SecretStr = SecretStr("")
    openai_scan_model: str = ""
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_scan_model: str = ""
    google_api_key: SecretStr = SecretStr("")
    google_scan_model: str = ""
    perplexity_api_key: SecretStr = SecretStr("")
    perplexity_scan_model: str = ""

    # --- Billing integrations (placeholders) ---
    appsumo_client_id: str = ""
    appsumo_client_secret: SecretStr = SecretStr("")
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_webhook_secret: SecretStr = SecretStr("")

    # --- Dev only ---
    dev_seed_enabled: bool = False

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, v: str) -> str:
        if isinstance(v, str):
            return v.upper()
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
