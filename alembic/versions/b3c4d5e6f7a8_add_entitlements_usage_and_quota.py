"""add entitlements usage and quota engine

Creates Phase 3 structures:
- plan_definitions: typed plan limits and feature flags
- plan_providers: allowed AI providers per plan
- workspace_usage_periods: monthly quota state per workspace
- quota_reservations: atomic AI Check reservations

Modifies:
- billing_accounts: add is_primary column + partial unique index
- usage_events: add idempotency_key (unique) + quota_reservation_id

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-08-18 15:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.types import UUIDType

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- plan_definitions ---
    op.create_table(
        "plan_definitions",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("max_projects", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_keywords_per_project", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_competitors_per_project", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_team_members", sa.Integer, nullable=False, server_default="0"),
        sa.Column("monthly_ai_checks", sa.Integer, nullable=False, server_default="0"),
        sa.Column("min_scheduled_scan_interval_hours", sa.Integer, nullable=True),
        sa.Column("confidence_scans_enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("verification_scans_enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("white_label_reports", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("exports_enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("agency_dashboard", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("integrations_enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("byok_enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("max_projects >= 0", name="ck_plan_definitions_max_projects_non_negative"),
        sa.CheckConstraint(
            "max_keywords_per_project >= 0",
            name="ck_plan_definitions_max_keywords_non_negative",
        ),
        sa.CheckConstraint(
            "max_competitors_per_project >= 0",
            name="ck_plan_definitions_max_competitors_non_negative",
        ),
        sa.CheckConstraint(
            "max_team_members >= 0", name="ck_plan_definitions_max_team_members_non_negative"
        ),
        sa.CheckConstraint(
            "monthly_ai_checks >= 0", name="ck_plan_definitions_monthly_ai_checks_non_negative"
        ),
        sa.CheckConstraint(
            "min_scheduled_scan_interval_hours IS NULL "
            "OR min_scheduled_scan_interval_hours > 0",
            name="ck_plan_definitions_scan_interval_positive",
        ),
    )
    op.create_index("ix_plan_definitions_code", "plan_definitions", ["code"], unique=True)

    # --- plan_providers ---
    op.create_table(
        "plan_providers",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column("plan_id", UUIDType, sa.ForeignKey("plan_definitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("plan_id", "provider", name="uq_plan_providers_plan_provider"),
    )
    op.create_index("ix_plan_providers_plan_id", "plan_providers", ["plan_id"])

    # --- billing_accounts: add is_primary ---
    op.add_column(
        "billing_accounts",
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    # Partial unique index: at most one primary billing account per workspace.
    op.create_index(
        "uq_billing_accounts_primary_per_workspace",
        "billing_accounts",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_primary = true"),
    )

    # --- workspace_usage_periods ---
    op.create_table(
        "workspace_usage_periods",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            UUIDType,
            sa.ForeignKey("workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ai_checks_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ai_checks_reserved", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "workspace_id", "period_start", name="uq_workspace_usage_period_workspace_period"
        ),
        sa.CheckConstraint(
            "ai_checks_used >= 0", name="ck_workspace_usage_periods_used_non_negative"
        ),
        sa.CheckConstraint(
            "ai_checks_reserved >= 0",
            name="ck_workspace_usage_periods_reserved_non_negative",
        ),
    )
    op.create_index("ix_workspace_usage_periods_workspace_id", "workspace_usage_periods", ["workspace_id"])
    op.create_index("ix_workspace_usage_periods_period_start", "workspace_usage_periods", ["period_start"])

    # --- quota_reservations ---
    op.create_table(
        "quota_reservations",
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
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            UUIDType,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("ai_checks_reserved", sa.Integer, nullable=False),
        sa.Column("ai_checks_committed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("idempotency_key", name="uq_quota_reservations_idempotency_key"),
        sa.CheckConstraint(
            "ai_checks_reserved > 0", name="ck_quota_reservations_reserved_positive"
        ),
        sa.CheckConstraint(
            "ai_checks_committed >= 0", name="ck_quota_reservations_committed_non_negative"
        ),
        sa.CheckConstraint(
            "ai_checks_committed <= ai_checks_reserved",
            name="ck_quota_reservations_committed_le_reserved",
        ),
    )
    op.create_index("ix_quota_reservations_workspace_id", "quota_reservations", ["workspace_id"])
    op.create_index("ix_quota_reservations_project_id", "quota_reservations", ["project_id"])
    op.create_index("ix_quota_reservations_user_id", "quota_reservations", ["user_id"])
    op.create_index("ix_quota_reservations_status", "quota_reservations", ["status"])

    # --- usage_events: add idempotency_key + quota_reservation_id ---
    op.add_column(
        "usage_events",
        sa.Column("idempotency_key", sa.String(255), nullable=True),
    )
    op.add_column(
        "usage_events",
        sa.Column("quota_reservation_id", UUIDType, nullable=True),
    )
    op.create_index("ix_usage_events_idempotency_key", "usage_events", ["idempotency_key"])
    op.create_index("ix_usage_events_quota_reservation_id", "usage_events", ["quota_reservation_id"])
    # Unique constraint on idempotency_key (NULLs allowed, multiple NULLs OK in Postgres).
    op.create_unique_constraint(
        "uq_usage_events_idempotency_key", "usage_events", ["idempotency_key"]
    )


def downgrade() -> None:
    # --- usage_events ---
    op.drop_constraint("uq_usage_events_idempotency_key", "usage_events", type_="unique")
    op.drop_index("ix_usage_events_quota_reservation_id", table_name="usage_events")
    op.drop_index("ix_usage_events_idempotency_key", table_name="usage_events")
    op.drop_column("usage_events", "quota_reservation_id")
    op.drop_column("usage_events", "idempotency_key")

    # --- quota_reservations ---
    op.drop_index("ix_quota_reservations_status", table_name="quota_reservations")
    op.drop_index("ix_quota_reservations_user_id", table_name="quota_reservations")
    op.drop_index("ix_quota_reservations_project_id", table_name="quota_reservations")
    op.drop_index("ix_quota_reservations_workspace_id", table_name="quota_reservations")
    op.drop_table("quota_reservations")

    # --- workspace_usage_periods ---
    op.drop_index("ix_workspace_usage_periods_period_start", table_name="workspace_usage_periods")
    op.drop_index("ix_workspace_usage_periods_workspace_id", table_name="workspace_usage_periods")
    op.drop_table("workspace_usage_periods")

    # --- billing_accounts: remove is_primary ---
    op.drop_index("uq_billing_accounts_primary_per_workspace", table_name="billing_accounts")
    op.drop_column("billing_accounts", "is_primary")

    # --- plan_providers ---
    op.drop_index("ix_plan_providers_plan_id", table_name="plan_providers")
    op.drop_table("plan_providers")

    # --- plan_definitions ---
    op.drop_index("ix_plan_definitions_code", table_name="plan_definitions")
    op.drop_table("plan_definitions")
