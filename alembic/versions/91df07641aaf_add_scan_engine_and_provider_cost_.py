"""add scan engine and provider cost accounting

Revision ID: 91df07641aaf
Revises: d5e6f7a8b9c0
Create Date: 2026-08-19 16:43:44.328255
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.types import UUIDType

revision: str = "91df07641aaf"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_MONEY = sa.Numeric(18, 10)


def upgrade() -> None:
    op.create_table(
        "provider_price_rules",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column("pricing_key", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_surface", sa.String(40), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_per_million_usd", _MONEY, nullable=True),
        sa.Column("cached_input_per_million_usd", _MONEY, nullable=True),
        sa.Column("cache_write_per_million_usd", _MONEY, nullable=True),
        sa.Column("output_per_million_usd", _MONEY, nullable=True),
        sa.Column("reasoning_per_million_usd", _MONEY, nullable=True),
        sa.Column("citation_per_million_usd", _MONEY, nullable=True),
        sa.Column("search_per_1000_usd", _MONEY, nullable=True),
        sa.Column("request_fee_usd", _MONEY, nullable=True),
        sa.Column("input_tokens_include_cached", sa.Boolean(), nullable=False),
        sa.Column("output_tokens_include_reasoning", sa.Boolean(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_url", sa.String(2000), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("pricing_key", name="uq_provider_price_rules_pricing_key"),
        sa.UniqueConstraint(
            "provider",
            "provider_surface",
            "model",
            "effective_from",
            name="uq_provider_price_rules_exact_effective",
        ),
        sa.CheckConstraint("model <> ''", name="ck_provider_price_rules_model_non_empty"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_provider_price_rules_effective_range",
        ),
        sa.CheckConstraint(
            "input_per_million_usd IS NULL OR input_per_million_usd >= 0",
            name="ck_provider_price_rules_input_non_negative",
        ),
        sa.CheckConstraint(
            "cached_input_per_million_usd IS NULL OR cached_input_per_million_usd >= 0",
            name="ck_provider_price_rules_cached_input_non_negative",
        ),
        sa.CheckConstraint(
            "cache_write_per_million_usd IS NULL OR cache_write_per_million_usd >= 0",
            name="ck_provider_price_rules_cache_write_non_negative",
        ),
        sa.CheckConstraint(
            "output_per_million_usd IS NULL OR output_per_million_usd >= 0",
            name="ck_provider_price_rules_output_non_negative",
        ),
        sa.CheckConstraint(
            "reasoning_per_million_usd IS NULL OR reasoning_per_million_usd >= 0",
            name="ck_provider_price_rules_reasoning_non_negative",
        ),
        sa.CheckConstraint(
            "citation_per_million_usd IS NULL OR citation_per_million_usd >= 0",
            name="ck_provider_price_rules_citation_non_negative",
        ),
        sa.CheckConstraint(
            "search_per_1000_usd IS NULL OR search_per_1000_usd >= 0",
            name="ck_provider_price_rules_search_non_negative",
        ),
        sa.CheckConstraint(
            "request_fee_usd IS NULL OR request_fee_usd >= 0",
            name="ck_provider_price_rules_request_fee_non_negative",
        ),
    )
    op.create_index("ix_provider_price_rules_provider", "provider_price_rules", ["provider"])

    op.create_table(
        "scans",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            UUIDType,
            sa.ForeignKey("workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            UUIDType,
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "prompt_set_id",
            UUIDType,
            sa.ForeignKey("prompt_sets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scan_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column(
            "requested_by_user_id",
            UUIDType,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column(
            "quota_reservation_id",
            UUIDType,
            sa.ForeignKey("quota_reservations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("prompt_count", sa.Integer(), nullable=False),
        sa.Column("provider_count", sa.Integer(), nullable=False),
        sa.Column("planned_ai_checks", sa.Integer(), nullable=False),
        sa.Column("successful_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(50), nullable=True),
        sa.Column("failure_message", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_scans_workspace_idempotency"),
        sa.UniqueConstraint("quota_reservation_id", name="uq_scans_quota_reservation"),
        sa.CheckConstraint("prompt_count > 0", name="ck_scans_prompt_count_positive"),
        sa.CheckConstraint("provider_count > 0", name="ck_scans_provider_count_positive"),
        sa.CheckConstraint("planned_ai_checks > 0", name="ck_scans_planned_checks_positive"),
        sa.CheckConstraint("successful_runs >= 0", name="ck_scans_successful_runs_non_negative"),
        sa.CheckConstraint("failed_runs >= 0", name="ck_scans_failed_runs_non_negative"),
        sa.CheckConstraint(
            "successful_runs + failed_runs <= planned_ai_checks",
            name="ck_scans_terminal_runs_within_plan",
        ),
    )
    for column in ("workspace_id", "project_id", "prompt_set_id", "quota_reservation_id", "status"):
        op.create_index(f"ix_scans_{column}", "scans", [column])

    op.create_table(
        "prompt_runs",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column("scan_id", UUIDType, sa.ForeignKey("scans.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("prompt_id", UUIDType, sa.ForeignKey("prompts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_surface", sa.String(40), nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column("requested_model", sa.String(255), nullable=False),
        sa.Column("returned_model", sa.String(255), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("provider_request_id", sa.String(255), nullable=True),
        sa.Column("provider_response_id", sa.String(255), nullable=True),
        sa.Column("finish_reason", sa.String(100), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("search_used", sa.Boolean(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_input_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("citation_tokens", sa.Integer(), nullable=True),
        sa.Column("search_requests", sa.Integer(), nullable=True),
        sa.Column("provider_reported_cost_usd", _MONEY, nullable=True),
        sa.Column("calculated_cost_usd", _MONEY, nullable=True),
        sa.Column("cost_usd", _MONEY, nullable=True),
        sa.Column("cost_source", sa.String(30), nullable=True),
        sa.Column(
            "pricing_rule_id",
            UUIDType,
            sa.ForeignKey("provider_price_rules.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("usage_event_id", UUIDType, nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("error_message", sa.String(1000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "scan_id", "prompt_id", "provider", "attempt_number",
            name="uq_prompt_runs_scan_prompt_provider_attempt",
        ),
        sa.UniqueConstraint("usage_event_id", name="uq_prompt_runs_usage_event"),
        sa.CheckConstraint("attempt_number > 0", name="ck_prompt_runs_attempt_positive"),
        *[
            sa.CheckConstraint(
                f"{column} IS NULL OR {column} >= 0", name=f"ck_prompt_runs_{name}_non_negative"
            )
            for column, name in (
                ("latency_ms", "latency"),
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("total_tokens", "total_tokens"),
                ("cached_input_tokens", "cached_tokens"),
                ("cache_write_input_tokens", "cache_write_tokens"),
                ("reasoning_tokens", "reasoning_tokens"),
                ("citation_tokens", "citation_tokens"),
                ("search_requests", "search_requests"),
                ("provider_reported_cost_usd", "provider_cost"),
                ("calculated_cost_usd", "calculated_cost"),
                ("cost_usd", "cost"),
            )
        ],
    )
    for column in ("scan_id", "prompt_id", "provider", "status"):
        op.create_index(f"ix_prompt_runs_{column}", "prompt_runs", [column])

    op.create_table(
        "response_sources",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column(
            "prompt_run_id",
            UUIDType,
            sa.ForeignKey("prompt_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(1000), nullable=True),
        sa.Column("source_type", sa.String(100), nullable=True),
        sa.Column("start_index", sa.Integer(), nullable=True),
        sa.Column("end_index", sa.Integer(), nullable=True),
        sa.Column("cited_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("prompt_run_id", "ordinal", name="uq_response_sources_run_ordinal"),
        sa.CheckConstraint("ordinal > 0", name="ck_response_sources_ordinal_positive"),
        sa.CheckConstraint(
            "start_index IS NULL OR start_index >= 0",
            name="ck_response_sources_start_index_non_negative",
        ),
        sa.CheckConstraint(
            "end_index IS NULL OR end_index >= 0",
            name="ck_response_sources_end_index_non_negative",
        ),
        sa.CheckConstraint(
            "start_index IS NULL OR end_index IS NULL OR end_index >= start_index",
            name="ck_response_sources_index_order",
        ),
    )
    op.create_index("ix_response_sources_prompt_run_id", "response_sources", ["prompt_run_id"])

    for name in (
        "cached_input_tokens",
        "cache_write_input_tokens",
        "reasoning_tokens",
        "citation_tokens",
        "search_requests",
    ):
        op.add_column("usage_events", sa.Column(name, sa.Integer(), nullable=True))
    op.add_column("usage_events", sa.Column("provider_reported_cost_usd", _MONEY, nullable=True))
    op.add_column("usage_events", sa.Column("cost_source", sa.String(30), nullable=True))
    op.add_column("usage_events", sa.Column("pricing_rule_id", UUIDType, nullable=True))
    op.add_column("usage_events", sa.Column("prompt_run_id", UUIDType, nullable=True))
    op.alter_column(
        "usage_events",
        "cost_usd",
        existing_type=sa.Numeric(12, 6),
        type_=_MONEY,
        nullable=True,
    )
    op.create_index("ix_usage_events_pricing_rule_id", "usage_events", ["pricing_rule_id"])
    op.create_index("ix_usage_events_prompt_run_id", "usage_events", ["prompt_run_id"])
    op.create_unique_constraint("uq_usage_events_prompt_run", "usage_events", ["prompt_run_id"])
    op.create_foreign_key(
        "fk_usage_events_pricing_rule_id",
        "usage_events",
        "provider_price_rules",
        ["pricing_rule_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_usage_events_prompt_run_id",
        "usage_events",
        "prompt_runs",
        ["prompt_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_prompt_runs_usage_event_id",
        "prompt_runs",
        "usage_events",
        ["usage_event_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    for column, name in (
        ("cached_input_tokens", "cached_input_tokens"),
        ("cache_write_input_tokens", "cache_write_tokens"),
        ("reasoning_tokens", "reasoning_tokens"),
        ("citation_tokens", "citation_tokens"),
        ("search_requests", "search_requests"),
        ("provider_reported_cost_usd", "provider_cost"),
    ):
        op.create_check_constraint(
            f"ck_usage_events_{name}_non_negative",
            "usage_events",
            f"{column} IS NULL OR {column} >= 0",
        )


def downgrade() -> None:
    op.drop_constraint("fk_prompt_runs_usage_event_id", "prompt_runs", type_="foreignkey")
    for name in (
        "search_requests",
        "reasoning_tokens",
        "provider_cost",
        "citation_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
    ):
        op.drop_constraint(f"ck_usage_events_{name}_non_negative", "usage_events", type_="check")
    op.drop_constraint("fk_usage_events_prompt_run_id", "usage_events", type_="foreignkey")
    op.drop_constraint("fk_usage_events_pricing_rule_id", "usage_events", type_="foreignkey")
    op.drop_constraint("uq_usage_events_prompt_run", "usage_events", type_="unique")
    op.drop_index("ix_usage_events_prompt_run_id", table_name="usage_events")
    op.drop_index("ix_usage_events_pricing_rule_id", table_name="usage_events")
    op.execute("UPDATE usage_events SET cost_usd = 0 WHERE cost_usd IS NULL")
    op.alter_column(
        "usage_events",
        "cost_usd",
        existing_type=_MONEY,
        type_=sa.Numeric(12, 6),
        nullable=False,
    )
    for name in (
        "prompt_run_id",
        "pricing_rule_id",
        "cost_source",
        "provider_reported_cost_usd",
        "search_requests",
        "citation_tokens",
        "reasoning_tokens",
        "cache_write_input_tokens",
        "cached_input_tokens",
    ):
        op.drop_column("usage_events", name)

    op.drop_index("ix_response_sources_prompt_run_id", table_name="response_sources")
    op.drop_table("response_sources")
    for column in ("status", "provider", "prompt_id", "scan_id"):
        op.drop_index(f"ix_prompt_runs_{column}", table_name="prompt_runs")
    op.drop_table("prompt_runs")
    for column in ("status", "quota_reservation_id", "prompt_set_id", "project_id", "workspace_id"):
        op.drop_index(f"ix_scans_{column}", table_name="scans")
    op.drop_table("scans")
    op.drop_index("ix_provider_price_rules_provider", table_name="provider_price_rules")
    op.drop_table("provider_price_rules")
