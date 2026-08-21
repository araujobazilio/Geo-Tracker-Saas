"""add opportunity verification and implementation baseline

Revision ID: c8f1d2a3e4b5
Revises: b72aec534f9c
Create Date: 2026-08-21 02:00:00.000000

Phase 10 — Verification Scans and Opportunity Outcome Tracking.

Schema changes:
- opportunities.implementation_baseline_occurrence_id: nullable UUID FK
  to opportunity_occurrences.id (ON DELETE RESTRICT).  Frozen when an
  Opportunity transitions to IMPLEMENTED; cleared when returning to
  IN_PROGRESS.
- opportunity_verifications: new table storing one verification
  comparison record per targeted VERIFICATION scan.  Links the frozen
  baseline occurrence + baseline STANDARD scan to the verification scan.
  Stores the deterministic before/after metric, outcome
  (VerificationOutcome), and quality-gate metadata.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f1d2a3e4b5"
down_revision: str | Sequence[str] | None = "b72aec534f9c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- opportunities.implementation_baseline_occurrence_id ---
    op.add_column(
        "opportunities",
        sa.Column(
            "implementation_baseline_occurrence_id",
            sa.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_opportunities_impl_baseline_occurrence",
        "opportunities",
        "opportunity_occurrences",
        ["implementation_baseline_occurrence_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- opportunity_verifications ---
    op.create_table(
        "opportunity_verifications",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "opportunity_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("opportunities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "baseline_occurrence_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("opportunity_occurrences.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "baseline_scan_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("scans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "verification_scan_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("scans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("verification_methodology_version", sa.String(50), nullable=False),
        sa.Column(
            "outcome",
            sa.String(20),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("reason_code", sa.String(50), nullable=True),
        sa.Column("evaluation_message", sa.String(1000), nullable=True),
        sa.Column("metric_name", sa.String(100), nullable=False),
        sa.Column("baseline_value", sa.Numeric(10, 4), nullable=True),
        sa.Column("verification_value", sa.Numeric(10, 4), nullable=True),
        sa.Column("delta_value", sa.Numeric(10, 4), nullable=True),
        sa.Column("baseline_brand_value", sa.Numeric(10, 4), nullable=True),
        sa.Column("verification_brand_value", sa.Numeric(10, 4), nullable=True),
        sa.Column("baseline_coverage", sa.Numeric(10, 4), nullable=True),
        sa.Column("verification_coverage", sa.Numeric(10, 4), nullable=True),
        sa.Column("resolution_threshold", sa.Numeric(10, 4), nullable=True),
        sa.Column("meaningful_improvement_threshold", sa.Numeric(10, 4), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
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
            "workspace_id",
            "idempotency_key",
            name="uq_opportunity_verifications_workspace_idempotency",
        ),
        sa.UniqueConstraint(
            "verification_scan_id",
            name="uq_opportunity_verifications_verification_scan",
        ),
    )
    op.create_index(
        "ix_opportunity_verifications_workspace_id",
        "opportunity_verifications",
        ["workspace_id"],
    )
    op.create_index(
        "ix_opportunity_verifications_project_id",
        "opportunity_verifications",
        ["project_id"],
    )
    op.create_index(
        "ix_opportunity_verifications_opportunity_id",
        "opportunity_verifications",
        ["opportunity_id"],
    )
    op.create_index(
        "ix_opportunity_verifications_outcome",
        "opportunity_verifications",
        ["outcome"],
    )
    op.create_index(
        "ix_opportunity_verifications_opportunity_created",
        "opportunity_verifications",
        ["opportunity_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_opportunity_verifications_opportunity_created",
        table_name="opportunity_verifications",
    )
    op.drop_index("ix_opportunity_verifications_outcome", table_name="opportunity_verifications")
    op.drop_index(
        "ix_opportunity_verifications_opportunity_id",
        table_name="opportunity_verifications",
    )
    op.drop_index(
        "ix_opportunity_verifications_project_id",
        table_name="opportunity_verifications",
    )
    op.drop_index(
        "ix_opportunity_verifications_workspace_id",
        table_name="opportunity_verifications",
    )
    op.drop_table("opportunity_verifications")
    op.drop_constraint(
        "fk_opportunities_impl_baseline_occurrence",
        "opportunities",
        type_="foreignkey",
    )
    op.drop_column("opportunities", "implementation_baseline_occurrence_id")
