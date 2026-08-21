"""GEO Tracker — application configuration.

All configuration is environment-driven via pydantic-settings.
Secrets must NEVER be hardcoded here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Known insecure placeholder values that must never be accepted in
# staging or production. They are only permitted in development/test.
_INSECURE_PLACEHOLDERS = frozenset(
    {
        "change-me",
        "change-me-to-a-long-random-string",
    }
)

# Minimum length for a production-grade secret key.
_MIN_SECRET_LENGTH = 32


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

    # --- Session / cookie ---
    session_cookie_name: str = "geo_session"
    session_ttl_seconds: int = 7 * 24 * 3600  # 7 days
    session_cookie_secure: bool = False  # enforced True in staging/production
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    csrf_cookie_name: str = "geo_csrf"

    # --- Rate limiting (auth endpoints) ---
    rate_limit_login_max: int = 8
    rate_limit_login_window_seconds: int = 300  # 5 minutes
    rate_limit_register_max: int = 5
    rate_limit_register_window_seconds: int = 3600  # 1 hour

    # --- Quota ---
    quota_reservation_ttl_seconds: int = 1800  # 30 minutes default

    # --- Scan engine ---
    scan_max_concurrency: int = 4
    scan_reservation_ttl_seconds: int = 21600
    scan_stale_after_seconds: int = 7200
    pricing_require_rule_for_execution: bool = True
    celery_broker_url: str = ""

    # --- Confidence scans (Phase 8) ---
    confidence_scan_default_repeats: int = 3
    confidence_scan_max_repeats: int = 5

    # --- AI providers ---
    openai_api_key: SecretStr = SecretStr("")
    openai_scan_model: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_web_search_max_tool_calls: int = 3
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_scan_model: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_web_search_tool_version: str = "web_search_20260318"
    anthropic_web_search_max_uses: int = 5
    google_api_key: SecretStr = SecretStr("")
    google_scan_model: str = ""
    google_base_url: str = "https://generativelanguage.googleapis.com"
    perplexity_api_key: SecretStr = SecretStr("")
    perplexity_scan_model: str = ""
    perplexity_base_url: str = "https://api.perplexity.ai"

    # --- Provider HTTP settings ---
    provider_connect_timeout_seconds: float = 10.0
    provider_read_timeout_seconds: float = 120.0
    provider_max_output_tokens: int = 4096

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

    @field_validator("provider_connect_timeout_seconds", "provider_read_timeout_seconds")
    @classmethod
    def _validate_positive_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Provider timeout must be positive.")
        return v

    @field_validator("provider_max_output_tokens")
    @classmethod
    def _validate_positive_max_tokens(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Provider max output tokens must be positive.")
        return v

    @field_validator(
        "anthropic_web_search_max_uses",
        "openai_web_search_max_tool_calls",
        "quota_reservation_ttl_seconds",
        "scan_max_concurrency",
        "scan_reservation_ttl_seconds",
        "scan_stale_after_seconds",
        "confidence_scan_default_repeats",
        "confidence_scan_max_repeats",
    )
    @classmethod
    def _validate_positive_integer_setting(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Configuration value must be positive.")
        return v

    @model_validator(mode="after")
    def _validate_production_secret(self) -> Settings:
        """Fail fast when APP_SECRET_KEY is unsafe in staging/production.

        - Development/test: placeholders and short values are allowed.
        - Staging/production: empty, known placeholder, or too-short
          secrets are rejected at config load time.

        The real secret value is NEVER included in the error message.
        """
        if self.app_env in {"development", "test"}:
            self._validate_confidence_scan_settings()
            return self

        secret = self.app_secret_key.get_secret_value()
        if not secret:
            raise ValueError(
                "APP_SECRET_KEY must be set in staging/production (it is currently empty)."
            )
        if secret.lower() in _INSECURE_PLACEHOLDERS:
            raise ValueError(
                "APP_SECRET_KEY is set to a known insecure placeholder. "
                "Provide a strong, unique secret for staging/production."
            )
        if len(secret) < _MIN_SECRET_LENGTH:
            raise ValueError(
                f"APP_SECRET_KEY must be at least {_MIN_SECRET_LENGTH} "
                "characters in staging/production."
            )
        # Enforce secure cookies in staging/production.
        if not self.session_cookie_secure:
            self.session_cookie_secure = True
        self._validate_confidence_scan_settings()
        return self

    def _validate_confidence_scan_settings(self) -> None:
        """Validate confidence scan repeat-count configuration bounds.

        - default >= 2 (a confidence scan must repeat at least twice)
        - max >= default
        - max <= 10 (absolute upper bound to prevent operational abuse)
        """
        if self.confidence_scan_default_repeats < 2:
            raise ValueError("CONFIDENCE_SCAN_DEFAULT_REPEATS must be >= 2.")
        if self.confidence_scan_max_repeats < self.confidence_scan_default_repeats:
            raise ValueError(
                "CONFIDENCE_SCAN_MAX_REPEATS must be >= CONFIDENCE_SCAN_DEFAULT_REPEATS."
            )
        if self.confidence_scan_max_repeats > 10:
            raise ValueError(
                "CONFIDENCE_SCAN_MAX_REPEATS must be <= 10 to prevent operational abuse."
            )

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_staging(self) -> bool:
        return self.app_env == "staging"

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
