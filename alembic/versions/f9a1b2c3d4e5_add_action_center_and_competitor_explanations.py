"""add action center and competitor explanations

Revision ID: f9a1b2c3d4e5
Revises: e7f8a9b0c1d2
Create Date: 2026-08-20 20:00:00.000000

Phase 9 — Evidence-Based Competitor Explanation and Action Center.

Schema changes:
- opportunities: logical, deduplicated workflow entities with stable
  fingerprint (project_id, fingerprint). Preserves human status across
  automated refreshes.
- opportunity_occurrences: immutable per-Scan detection records.
  Unique (opportunity_id, scan_id).
- opportunity_evidence: typed evidence rows linking occurrences to
  persisted Scan evidence (PromptRun, ResponseSource, metric gaps).
  Unique (occurrence_id, evidence_key).

All FKs use ON DELETE RESTRICT to preserve evidence lineage.
No hard-delete API.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import UUIDType

revision: str = "f9a1b2c3d4e5"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- opportunities ---
    op.create_table(
        "opportunities",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column("workspace_id", UUIDType, nullable=False),
        sa.Column("project_id", UUIDType, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("opportunity_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="OPEN"),
        sa.Column("priority", sa.String(10), nullable=False),
        sa.Column("action_engine_version", sa.String(50), nullable=False),
        sa.Column("competitor_entity_key", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(20), nullable=True),
        sa.Column("prompt_id", UUIDType, nullable=True),
        sa.Column("prompt_type", sa.String(20), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("recommended_action", sa.Text(), nullable=False),
        sa.Column("first_detected_scan_id", UUIDType, nullable=False),
        sa.Column("latest_detected_scan_id", UUIDType, nullable=False),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("implemented_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissal_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "project_id", "fingerprint", name="uq_opportunities_project_fingerprint"
        ),
    )
    op.create_index("ix_opportunities_workspace_id", "opportunities", ["workspace_id"])
    op.create_index("ix_opportunities_project_id", "opportunities", ["project_id"])
    op.create_index("ix_opportunities_opportunity_type", "opportunities", ["opportunity_type"])
    op.create_index("ix_opportunities_status", "opportunities", ["status"])
    op.create_index("ix_opportunities_priority", "opportunities", ["priority"])
    op.create_index("ix_opportunities_provider", "opportunities", ["provider"])
    op.create_index("ix_opportunities_prompt_id", "opportunities", ["prompt_id"])
    op.create_foreign_key(
        "fk_opportunities_workspace_id_workspaces",
        "opportunities",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_opportunities_project_id_projects",
        "opportunities",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_opportunities_prompt_id_prompts",
        "opportunities",
        "prompts",
        ["prompt_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_opportunities_first_detected_scan_id_scans",
        "opportunities",
        "scans",
        ["first_detected_scan_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_opportunities_latest_detected_scan_id_scans",
        "opportunities",
        "scans",
        ["latest_detected_scan_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- opportunity_occurrences ---
    op.create_table(
        "opportunity_occurrences",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column("opportunity_id", UUIDType, nullable=False),
        sa.Column("scan_id", UUIDType, nullable=False),
        sa.Column("scan_analysis_id", UUIDType, nullable=False),
        sa.Column("competitor_entity_snapshot_id", UUIDType, nullable=True),
        sa.Column("brand_entity_snapshot_id", UUIDType, nullable=False),
        sa.Column("priority_at_detection", sa.String(10), nullable=False),
        sa.Column("brand_visibility", sa.Numeric(10, 4), nullable=True),
        sa.Column("competitor_visibility", sa.Numeric(10, 4), nullable=True),
        sa.Column("visibility_gap_pp", sa.Numeric(10, 4), nullable=True),
        sa.Column("brand_citation_rate", sa.Numeric(10, 4), nullable=True),
        sa.Column("competitor_citation_rate", sa.Numeric(10, 4), nullable=True),
        sa.Column("citation_gap_pp", sa.Numeric(10, 4), nullable=True),
        sa.Column("measurement_coverage", sa.Numeric(10, 4), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "opportunity_id", "scan_id", name="uq_opportunity_occurrences_opp_scan"
        ),
    )
    op.create_index(
        "ix_opportunity_occurrences_opportunity_id",
        "opportunity_occurrences",
        ["opportunity_id"],
    )
    op.create_index(
        "ix_opportunity_occurrences_scan_id",
        "opportunity_occurrences",
        ["scan_id"],
    )
    op.create_index(
        "ix_opportunity_occurrences_scan_analysis_id",
        "opportunity_occurrences",
        ["scan_analysis_id"],
    )
    op.create_foreign_key(
        "fk_opportunity_occurrences_opportunity_id_opportunities",
        "opportunity_occurrences",
        "opportunities",
        ["opportunity_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_opportunity_occurrences_scan_id_scans",
        "opportunity_occurrences",
        "scans",
        ["scan_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_opportunity_occurrences_scan_analysis_id_scan_analyses",
        "opportunity_occurrences",
        "scan_analyses",
        ["scan_analysis_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_opportunity_occurrences_competitor_snapshot_id_snapshots",
        "opportunity_occurrences",
        "scan_entity_snapshots",
        ["competitor_entity_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_opportunity_occurrences_brand_snapshot_id_snapshots",
        "opportunity_occurrences",
        "scan_entity_snapshots",
        ["brand_entity_snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- opportunity_evidence ---
    op.create_table(
        "opportunity_evidence",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column("occurrence_id", UUIDType, nullable=False),
        sa.Column("evidence_key", sa.String(255), nullable=False),
        sa.Column("evidence_type", sa.String(30), nullable=False),
        sa.Column("prompt_id", UUIDType, nullable=True),
        sa.Column("prompt_run_id", UUIDType, nullable=True),
        sa.Column("response_source_id", UUIDType, nullable=True),
        sa.Column("provider", sa.String(20), nullable=True),
        sa.Column("metric_name", sa.String(100), nullable=True),
        sa.Column("brand_value", sa.Numeric(10, 4), nullable=True),
        sa.Column("competitor_value", sa.Numeric(10, 4), nullable=True),
        sa.Column("delta_value", sa.Numeric(10, 4), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "occurrence_id", "evidence_key", name="uq_opportunity_evidence_occ_key"
        ),
    )
    op.create_index(
        "ix_opportunity_evidence_occurrence_id",
        "opportunity_evidence",
        ["occurrence_id"],
    )
    op.create_foreign_key(
        "fk_opportunity_evidence_occurrence_id_occurrences",
        "opportunity_evidence",
        "opportunity_occurrences",
        ["occurrence_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_opportunity_evidence_prompt_id_prompts",
        "opportunity_evidence",
        "prompts",
        ["prompt_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_opportunity_evidence_prompt_run_id_prompt_runs",
        "opportunity_evidence",
        "prompt_runs",
        ["prompt_run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_opportunity_evidence_response_source_id_response_sources",
        "opportunity_evidence",
        "response_sources",
        ["response_source_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # --- opportunity_evidence ---
    op.drop_constraint(
        "fk_opportunity_evidence_response_source_id_response_sources",
        "opportunity_evidence",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_opportunity_evidence_prompt_run_id_prompt_runs",
        "opportunity_evidence",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_opportunity_evidence_prompt_id_prompts",
        "opportunity_evidence",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_opportunity_evidence_occurrence_id_occurrences",
        "opportunity_evidence",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_opportunity_evidence_occurrence_id", table_name="opportunity_evidence"
    )
    op.drop_table("opportunity_evidence")

    # --- opportunity_occurrences ---
    op.drop_constraint(
        "fk_opportunity_occurrences_brand_snapshot_id_snapshots",
        "opportunity_occurrences",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_opportunity_occurrences_competitor_snapshot_id_snapshots",
        "opportunity_occurrences",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_opportunity_occurrences_scan_analysis_id_scan_analyses",
        "opportunity_occurrences",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_opportunity_occurrences_scan_id_scans",
        "opportunity_occurrences",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_opportunity_occurrences_opportunity_id_opportunities",
        "opportunity_occurrences",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_opportunity_occurrences_scan_analysis_id",
        table_name="opportunity_occurrences",
    )
    op.drop_index(
        "ix_opportunity_occurrences_scan_id", table_name="opportunity_occurrences"
    )
    op.drop_index(
        "ix_opportunity_occurrences_opportunity_id",
        table_name="opportunity_occurrences",
    )
    op.drop_table("opportunity_occurrences")

    # --- opportunities ---
    op.drop_constraint(
        "fk_opportunities_latest_detected_scan_id_scans",
        "opportunities",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_opportunities_first_detected_scan_id_scans",
        "opportunities",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_opportunities_prompt_id_prompts", "opportunities", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_opportunities_project_id_projects", "opportunities", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_opportunities_workspace_id_workspaces", "opportunities", type_="foreignkey"
    )
    op.drop_index("ix_opportunities_prompt_id", table_name="opportunities")
    op.drop_index("ix_opportunities_provider", table_name="opportunities")
    op.drop_index("ix_opportunities_priority", table_name="opportunities")
    op.drop_index("ix_opportunities_status", table_name="opportunities")
    op.drop_index("ix_opportunities_opportunity_type", table_name="opportunities")
    op.drop_index("ix_opportunities_project_id", table_name="opportunities")
    op.drop_index("ix_opportunities_workspace_id", table_name="opportunities")
    op.drop_table("opportunities")
