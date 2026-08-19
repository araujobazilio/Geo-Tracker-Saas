"""Scan execution and immutable provider evidence models."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    CostSource,
    LLMProvider,
    PromptRunStatus,
    ProviderErrorCode,
    ProviderExecutionMode,
    ProviderSurface,
    ScanStatus,
    ScanType,
)
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.prompt_set import PromptSet
    from app.models.tracking import Prompt


class Scan(UUIDPrimaryKey, TimestampMixin, Base):
    """A reproducible execution plan for one exact PromptSet."""

    __tablename__ = "scans"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_scans_workspace_idempotency"),
        UniqueConstraint("quota_reservation_id", name="uq_scans_quota_reservation"),
        CheckConstraint("prompt_count > 0", name="ck_scans_prompt_count_positive"),
        CheckConstraint("provider_count > 0", name="ck_scans_provider_count_positive"),
        CheckConstraint("planned_ai_checks > 0", name="ck_scans_planned_checks_positive"),
        CheckConstraint("successful_runs >= 0", name="ck_scans_successful_runs_non_negative"),
        CheckConstraint("failed_runs >= 0", name="ck_scans_failed_runs_non_negative"),
        CheckConstraint(
            "successful_runs + failed_runs <= planned_ai_checks",
            name="ck_scans_terminal_runs_within_plan",
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    prompt_set_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("prompt_sets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    scan_type: Mapped[ScanType] = mapped_column(String(20), nullable=False)
    status: Mapped[ScanStatus] = mapped_column(
        String(20), nullable=False, default=ScanStatus.PENDING, index=True
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    quota_reservation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("quota_reservations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    prompt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_count: Mapped[int] = mapped_column(Integer, nullable=False)
    planned_ai_checks: Mapped[int] = mapped_column(Integer, nullable=False)
    successful_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    project: Mapped[Project] = relationship()
    prompt_set: Mapped[PromptSet] = relationship()
    runs: Mapped[list[PromptRun]] = relationship(back_populates="scan")


class PromptRun(UUIDPrimaryKey, Base):
    """One snapshotted Prompt and Provider execution attempt."""

    __tablename__ = "prompt_runs"
    __table_args__ = (
        UniqueConstraint(
            "scan_id",
            "prompt_id",
            "provider",
            "attempt_number",
            name="uq_prompt_runs_scan_prompt_provider_attempt",
        ),
        UniqueConstraint("usage_event_id", name="uq_prompt_runs_usage_event"),
        CheckConstraint("attempt_number > 0", name="ck_prompt_runs_attempt_positive"),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="ck_prompt_runs_latency_non_negative"
        ),
        CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name="ck_prompt_runs_input_tokens_non_negative",
        ),
        CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name="ck_prompt_runs_output_tokens_non_negative",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_prompt_runs_total_tokens_non_negative",
        ),
        CheckConstraint(
            "cached_input_tokens IS NULL OR cached_input_tokens >= 0",
            name="ck_prompt_runs_cached_tokens_non_negative",
        ),
        CheckConstraint(
            "cache_write_input_tokens IS NULL OR cache_write_input_tokens >= 0",
            name="ck_prompt_runs_cache_write_tokens_non_negative",
        ),
        CheckConstraint(
            "reasoning_tokens IS NULL OR reasoning_tokens >= 0",
            name="ck_prompt_runs_reasoning_tokens_non_negative",
        ),
        CheckConstraint(
            "citation_tokens IS NULL OR citation_tokens >= 0",
            name="ck_prompt_runs_citation_tokens_non_negative",
        ),
        CheckConstraint(
            "search_requests IS NULL OR search_requests >= 0",
            name="ck_prompt_runs_search_requests_non_negative",
        ),
        CheckConstraint(
            "provider_reported_cost_usd IS NULL OR provider_reported_cost_usd >= 0",
            name="ck_prompt_runs_provider_cost_non_negative",
        ),
        CheckConstraint(
            "calculated_cost_usd IS NULL OR calculated_cost_usd >= 0",
            name="ck_prompt_runs_calculated_cost_non_negative",
        ),
        CheckConstraint(
            "cost_usd IS NULL OR cost_usd >= 0", name="ck_prompt_runs_cost_non_negative"
        ),
    )

    scan_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("scans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("prompts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider: Mapped[LLMProvider] = mapped_column(String(30), nullable=False, index=True)
    provider_surface: Mapped[ProviderSurface] = mapped_column(String(40), nullable=False)
    execution_mode: Mapped[ProviderExecutionMode] = mapped_column(String(20), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(255), nullable=False)
    returned_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[PromptRunStatus] = mapped_column(
        String(20), nullable=False, default=PromptRunStatus.PENDING, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_response_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    search_used: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    citation_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    search_requests: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_reported_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True
    )
    calculated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 10), nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 10), nullable=True)
    cost_source: Mapped[CostSource | None] = mapped_column(String(30), nullable=True)
    pricing_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("provider_price_rules.id", ondelete="RESTRICT"), nullable=True
    )
    usage_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey(
            "usage_events.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_prompt_runs_usage_event_id",
        ),
        nullable=True,
    )
    error_code: Mapped[ProviderErrorCode | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    scan: Mapped[Scan] = relationship(back_populates="runs")
    prompt: Mapped[Prompt] = relationship()
    sources: Mapped[list[ResponseSource]] = relationship(
        back_populates="prompt_run", order_by="ResponseSource.ordinal"
    )


class ResponseSource(UUIDPrimaryKey, Base):
    """Provider-returned source evidence in deterministic response order."""

    __tablename__ = "response_sources"
    __table_args__ = (
        UniqueConstraint("prompt_run_id", "ordinal", name="uq_response_sources_run_ordinal"),
        CheckConstraint("ordinal > 0", name="ck_response_sources_ordinal_positive"),
        CheckConstraint(
            "start_index IS NULL OR start_index >= 0",
            name="ck_response_sources_start_index_non_negative",
        ),
        CheckConstraint(
            "end_index IS NULL OR end_index >= 0",
            name="ck_response_sources_end_index_non_negative",
        ),
        CheckConstraint(
            "start_index IS NULL OR end_index IS NULL OR end_index >= start_index",
            name="ck_response_sources_index_order",
        ),
    )

    prompt_run_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("prompt_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    start_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    prompt_run: Mapped[PromptRun] = relationship(back_populates="sources")
