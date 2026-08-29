"""add scheduled scans, notifications, and email delivery

Revision ID: e1a2b3c4d5e6
Revises: d9e0f1a2b3c4
Create Date: 2026-08-30 02:00:00.000000

Phase 11 — Scheduled Scans, Notification Outbox and Email Delivery.

Schema changes:
- project_scan_schedules table (one schedule per Project)
- notifications table (persisted in-app notification events)
- notification_preferences table (per-user, per-workspace)
- email_deliveries table (transactional outbox)
- scans.scan_schedule_id (nullable FK to project_scan_schedules)
- scans.scheduled_for (nullable timestamptz)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "d9e0f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- project_scan_schedules ---
    op.create_table(
        "project_scan_schedules",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("interval_hours", sa.Integer(), nullable=False),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("last_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_scan_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_outcome", sa.String(50), nullable=True),
        sa.Column("last_skip_reason", sa.String(200), nullable=True),
        sa.Column("created_by_user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("interval_hours > 0", name="ck_project_scan_schedules_interval_positive"),
        sa.UniqueConstraint("project_id", name="uq_project_scan_schedules_one_per_project"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["last_scan_id"], ["scans.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_project_scan_schedules_workspace_id",
        "project_scan_schedules",
        ["workspace_id"],
    )
    op.create_index(
        "ix_project_scan_schedules_next_run_at",
        "project_scan_schedules",
        ["next_run_at"],
    )
    op.create_index(
        "ix_project_scan_schedules_created_by_user_id",
        "project_scan_schedules",
        ["created_by_user_id"],
    )

    # --- notifications ---
    op.create_table(
        "notifications",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("message", sa.String(2000), nullable=False),
        sa.Column("project_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scan_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("opportunity_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verification_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deep_link_path", sa.String(500), nullable=True),
        sa.Column("dedup_key", sa.String(255), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "dedup_key", name="uq_notifications_user_dedup_key"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scan_id"], ["scans.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["verification_id"], ["opportunity_verifications.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_notifications_workspace_id", "notifications", ["workspace_id"])
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_notification_type", "notifications", ["notification_type"])

    # --- notification_preferences ---
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "scheduled_scan_summary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "high_priority_opportunities",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "verification_outcomes",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "workspace_id", "user_id", name="uq_notification_preferences_workspace_user"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_notification_preferences_workspace_id",
        "notification_preferences",
        ["workspace_id"],
    )
    op.create_index(
        "ix_notification_preferences_user_id",
        "notification_preferences",
        ["user_id"],
    )

    # --- email_deliveries ---
    op.create_table(
        "email_deliveries",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("notification_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_email", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("failure_message", sa.String(1000), nullable=True),
        sa.Column("message_id", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("notification_id", name="uq_email_deliveries_one_per_notification"),
        sa.ForeignKeyConstraint(["notification_id"], ["notifications.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_email_deliveries_status", "email_deliveries", ["status"])

    # --- scans: add schedule lineage columns ---
    op.add_column(
        "scans",
        sa.Column("scan_schedule_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "scans",
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_scans_scan_schedule_id",
        "scans",
        ["scan_schedule_id"],
    )
    op.create_foreign_key(
        "fk_scans_scan_schedule_id",
        "scans",
        "project_scan_schedules",
        ["scan_schedule_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_scans_scan_schedule_id", "scans", type_="foreignkey")
    op.drop_index("ix_scans_scan_schedule_id", table_name="scans")
    op.drop_column("scans", "scheduled_for")
    op.drop_column("scans", "scan_schedule_id")

    op.drop_index("ix_email_deliveries_status", table_name="email_deliveries")
    op.drop_table("email_deliveries")

    op.drop_index(
        "ix_notification_preferences_user_id", table_name="notification_preferences"
    )
    op.drop_index(
        "ix_notification_preferences_workspace_id", table_name="notification_preferences"
    )
    op.drop_table("notification_preferences")

    op.drop_index("ix_notifications_notification_type", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_index("ix_notifications_workspace_id", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index(
        "ix_project_scan_schedules_created_by_user_id", table_name="project_scan_schedules"
    )
    op.drop_index(
        "ix_project_scan_schedules_next_run_at", table_name="project_scan_schedules"
    )
    op.drop_index(
        "ix_project_scan_schedules_workspace_id", table_name="project_scan_schedules"
    )
    op.drop_table("project_scan_schedules")
