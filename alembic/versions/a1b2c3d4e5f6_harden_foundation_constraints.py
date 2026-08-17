"""harden foundation constraints

Add database-level integrity constraints:
- AppSumoLicense.external_license_id UNIQUE (one external license id per record)
- UsageEvent non-negative CHECK constraints for accounting integrity

Revision ID: a1b2c3d4e5f6
Revises: 2becd68d611b
Create Date: 2026-08-17 20:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "2becd68d611b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- AppSumoLicense.external_license_id uniqueness ---
    # An external AppSumo license id must identify at most one record.
    op.create_unique_constraint(
        "uq_appsumo_licenses_external_license_id",
        "appsumo_licenses",
        ["external_license_id"],
    )

    # --- UsageEvent non-negative CHECK constraints ---
    # Accounting integrity: negative values are invalid for billing/cost data.
    op.create_check_constraint(
        "ck_usage_events_ai_checks_non_negative",
        "usage_events",
        sa.text("ai_checks >= 0"),
    )
    op.create_check_constraint(
        "ck_usage_events_input_tokens_non_negative",
        "usage_events",
        sa.text("input_tokens IS NULL OR input_tokens >= 0"),
    )
    op.create_check_constraint(
        "ck_usage_events_output_tokens_non_negative",
        "usage_events",
        sa.text("output_tokens IS NULL OR output_tokens >= 0"),
    )
    op.create_check_constraint(
        "ck_usage_events_total_tokens_non_negative",
        "usage_events",
        sa.text("total_tokens IS NULL OR total_tokens >= 0"),
    )
    op.create_check_constraint(
        "ck_usage_events_cost_usd_non_negative",
        "usage_events",
        sa.text("cost_usd >= 0"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_usage_events_cost_usd_non_negative", "usage_events", type_="check")
    op.drop_constraint("ck_usage_events_total_tokens_non_negative", "usage_events", type_="check")
    op.drop_constraint("ck_usage_events_output_tokens_non_negative", "usage_events", type_="check")
    op.drop_constraint("ck_usage_events_input_tokens_non_negative", "usage_events", type_="check")
    op.drop_constraint("ck_usage_events_ai_checks_non_negative", "usage_events", type_="check")
    op.drop_constraint(
        "uq_appsumo_licenses_external_license_id", "appsumo_licenses", type_="unique"
    )
