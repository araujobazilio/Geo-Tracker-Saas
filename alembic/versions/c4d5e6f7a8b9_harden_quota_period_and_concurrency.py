"""harden quota period and concurrency integrity

Phase 3.1 — bind each QuotaReservation to its original WorkspaceUsagePeriod
and add FK on UsageEvent.quota_reservation_id.

Changes:
- quota_reservations: add usage_period_id (UUID NOT NULL, FK to
  workspace_usage_periods.id ON DELETE RESTRICT, indexed)
- usage_events: convert quota_reservation_id from plain UUID to real FK
  to quota_reservations.id ON DELETE RESTRICT

Backfill strategy for existing reservations:
  Derive the UTC calendar month from the reservation's created_at and
  match the corresponding workspace_usage_periods row by
  (workspace_id, period_start). If no match is found, fail explicitly
  rather than corrupting accounting.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-08-18 18:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.types import UUIDType

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- quota_reservations: add usage_period_id ---

    # Step 1: add the column as nullable so we can backfill.
    op.add_column(
        "quota_reservations",
        sa.Column("usage_period_id", UUIDType, nullable=True),
    )
    op.create_index(
        "ix_quota_reservations_usage_period_id",
        "quota_reservations",
        ["usage_period_id"],
    )

    # Step 2: backfill existing rows by deriving the UTC calendar month
    # from created_at and matching workspace_usage_periods.
    # date_trunc('month', created_at) gives the period_start.
    # If a reservation cannot be matched, the UPDATE will leave
    # usage_period_id NULL, and the subsequent NOT NULL constraint
    # addition will fail explicitly.
    op.execute(
        """
        UPDATE quota_reservations qr
        SET usage_period_id = wup.id
        FROM workspace_usage_periods wup
        WHERE qr.workspace_id = wup.workspace_id
          AND wup.period_start = date_trunc('month', qr.created_at)
        """
    )

    # Step 3: verify no rows are left unbackfilled. If any exist,
    # raise an error so the migration fails rather than corrupting data.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM quota_reservations WHERE usage_period_id IS NULL) THEN
                RAISE EXCEPTION
                    'Cannot make usage_period_id NOT NULL: % reservations could not be matched to a usage period',
                    (SELECT count(*) FROM quota_reservations WHERE usage_period_id IS NULL);
            END IF;
        END $$;
        """
    )

    # Step 4: add the FK constraint.
    op.create_foreign_key(
        "fk_quota_reservations_usage_period_id",
        "quota_reservations",
        "workspace_usage_periods",
        ["usage_period_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Step 5: make the column NOT NULL.
    op.alter_column(
        "quota_reservations",
        "usage_period_id",
        existing_type=UUIDType,
        nullable=False,
    )

    # --- usage_events: add FK on quota_reservation_id ---
    op.create_foreign_key(
        "fk_usage_events_quota_reservation_id",
        "usage_events",
        "quota_reservations",
        ["quota_reservation_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # --- usage_events: drop FK ---
    op.drop_constraint(
        "fk_usage_events_quota_reservation_id", "usage_events", type_="foreignkey"
    )

    # --- quota_reservations: drop NOT NULL, FK, index, column ---
    op.alter_column(
        "quota_reservations",
        "usage_period_id",
        existing_type=UUIDType,
        nullable=True,
    )
    op.drop_constraint(
        "fk_quota_reservations_usage_period_id", "quota_reservations", type_="foreignkey"
    )
    op.drop_index("ix_quota_reservations_usage_period_id", table_name="quota_reservations")
    op.drop_column("quota_reservations", "usage_period_id")
