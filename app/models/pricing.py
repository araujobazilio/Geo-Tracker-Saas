"""Immutable, versioned provider pricing evidence."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import LLMProvider, ProviderSurface
from app.db.base import Base
from app.db.mixins import UUIDPrimaryKey


class ProviderPriceRule(UUIDPrimaryKey, Base):
    """Append-only exact-model pricing effective for a bounded time range."""

    __tablename__ = "provider_price_rules"
    __table_args__ = (
        UniqueConstraint("pricing_key", name="uq_provider_price_rules_pricing_key"),
        UniqueConstraint(
            "provider",
            "provider_surface",
            "model",
            "effective_from",
            name="uq_provider_price_rules_exact_effective",
        ),
        CheckConstraint("model <> ''", name="ck_provider_price_rules_model_non_empty"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_provider_price_rules_effective_range",
        ),
        CheckConstraint(
            "input_per_million_usd IS NULL OR input_per_million_usd >= 0",
            name="ck_provider_price_rules_input_non_negative",
        ),
        CheckConstraint(
            "cached_input_per_million_usd IS NULL OR cached_input_per_million_usd >= 0",
            name="ck_provider_price_rules_cached_input_non_negative",
        ),
        CheckConstraint(
            "cache_write_per_million_usd IS NULL OR cache_write_per_million_usd >= 0",
            name="ck_provider_price_rules_cache_write_non_negative",
        ),
        CheckConstraint(
            "output_per_million_usd IS NULL OR output_per_million_usd >= 0",
            name="ck_provider_price_rules_output_non_negative",
        ),
        CheckConstraint(
            "reasoning_per_million_usd IS NULL OR reasoning_per_million_usd >= 0",
            name="ck_provider_price_rules_reasoning_non_negative",
        ),
        CheckConstraint(
            "citation_per_million_usd IS NULL OR citation_per_million_usd >= 0",
            name="ck_provider_price_rules_citation_non_negative",
        ),
        CheckConstraint(
            "search_per_1000_usd IS NULL OR search_per_1000_usd >= 0",
            name="ck_provider_price_rules_search_non_negative",
        ),
        CheckConstraint(
            "request_fee_usd IS NULL OR request_fee_usd >= 0",
            name="ck_provider_price_rules_request_fee_non_negative",
        ),
    )

    pricing_key: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[LLMProvider] = mapped_column(String(30), nullable=False, index=True)
    provider_surface: Mapped[ProviderSurface] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_per_million_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 10), nullable=True)
    cached_input_per_million_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True
    )
    cache_write_per_million_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True
    )
    output_per_million_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 10), nullable=True)
    reasoning_per_million_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True
    )
    citation_per_million_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 10), nullable=True)
    search_per_1000_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 10), nullable=True)
    request_fee_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 10), nullable=True)
    input_tokens_include_cached: Mapped[bool] = mapped_column(Boolean, nullable=False)
    output_tokens_include_reasoning: Mapped[bool] = mapped_column(Boolean, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
