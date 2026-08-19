"""add deterministic visibility analysis

Revision ID: d677b6e44e9b
Revises: 91df07641aaf
Create Date: 2026-08-19 23:00:00.000000

Creates four new tables for Phase 7 deterministic brand/competitor
detection, citation attribution, and visibility metrics:

- scan_entity_snapshots: immutable entity configuration at scan time
- scan_analyses: versioned analysis runs
- entity_mentions: detected entity occurrences in response text
- source_attributions: domain-based source attribution

No automatic legacy backfill is performed. Existing scans without
snapshots remain identifiable as not analyzable with historical fidelity.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import UUIDType

revision: str = "d677b6e44e9b"
down_revision: Union[str, None] = "91df07641aaf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scan_entity_snapshots",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column("scan_id", UUIDType, nullable=False),
        sa.Column("entity_key", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("aliases", sa.dialects.postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("source_competitor_id", UUIDType, nullable=True),
        sa.Column("ordinal", sa.Integer, nullable=False),
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
        sa.ForeignKeyConstraint(
            ["scan_id"], ["scans.id"], ondelete="RESTRICT", name="fk_scan_entity_snapshots_scan_id"
        ),
        sa.ForeignKeyConstraint(
            ["source_competitor_id"],
            ["competitors.id"],
            ondelete="SET NULL",
            name="fk_scan_entity_snapshots_source_competitor_id",
        ),
        sa.UniqueConstraint(
            "scan_id", "entity_key", name="uq_scan_entity_snapshots_scan_entity_key"
        ),
        sa.CheckConstraint("ordinal > 0", name="ck_scan_entity_snapshots_ordinal_positive"),
        sa.CheckConstraint("name <> ''", name="ck_scan_entity_snapshots_name_non_empty"),
        sa.CheckConstraint("domain <> ''", name="ck_scan_entity_snapshots_domain_non_empty"),
    )
    op.create_index(
        "ix_scan_entity_snapshots_scan_id", "scan_entity_snapshots", ["scan_id"]
    )
    op.create_index(
        "ix_scan_entity_snapshots_entity_type", "scan_entity_snapshots", ["entity_type"]
    )

    op.create_table(
        "scan_analyses",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column("scan_id", UUIDType, nullable=False),
        sa.Column("analysis_version", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(50), nullable=True),
        sa.Column("failure_message", sa.String(1000), nullable=True),
        sa.Column("warning_count", sa.Integer, nullable=False, server_default="0"),
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
        sa.ForeignKeyConstraint(
            ["scan_id"], ["scans.id"], ondelete="RESTRICT", name="fk_scan_analyses_scan_id"
        ),
        sa.UniqueConstraint(
            "scan_id", "analysis_version", name="uq_scan_analyses_scan_version"
        ),
        sa.CheckConstraint(
            "warning_count >= 0", name="ck_scan_analyses_warning_count_non_negative"
        ),
    )
    op.create_index("ix_scan_analyses_scan_id", "scan_analyses", ["scan_id"])
    op.create_index("ix_scan_analyses_status", "scan_analyses", ["status"])

    op.create_table(
        "entity_mentions",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column("scan_analysis_id", UUIDType, nullable=False),
        sa.Column("prompt_run_id", UUIDType, nullable=False),
        sa.Column("entity_snapshot_id", UUIDType, nullable=False),
        sa.Column("occurrence_index", sa.Integer, nullable=False),
        sa.Column("match_type", sa.String(20), nullable=False),
        sa.Column("matched_text", sa.Text, nullable=False),
        sa.Column("matched_term", sa.String(255), nullable=False),
        sa.Column("start_index", sa.Integer, nullable=False),
        sa.Column("end_index", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["scan_analysis_id"],
            ["scan_analyses.id"],
            ondelete="RESTRICT",
            name="fk_entity_mentions_scan_analysis_id",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_run_id"],
            ["prompt_runs.id"],
            ondelete="RESTRICT",
            name="fk_entity_mentions_prompt_run_id",
        ),
        sa.ForeignKeyConstraint(
            ["entity_snapshot_id"],
            ["scan_entity_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_entity_mentions_entity_snapshot_id",
        ),
        sa.UniqueConstraint(
            "scan_analysis_id",
            "prompt_run_id",
            "entity_snapshot_id",
            "occurrence_index",
            name="uq_entity_mentions_analysis_run_entity_occurrence",
        ),
        sa.CheckConstraint("occurrence_index > 0", name="ck_entity_mentions_occurrence_positive"),
        sa.CheckConstraint("start_index >= 0", name="ck_entity_mentions_start_non_negative"),
        sa.CheckConstraint("end_index > start_index", name="ck_entity_mentions_end_gt_start"),
    )
    op.create_index("ix_entity_mentions_scan_analysis_id", "entity_mentions", ["scan_analysis_id"])
    op.create_index("ix_entity_mentions_prompt_run_id", "entity_mentions", ["prompt_run_id"])
    op.create_index(
        "ix_entity_mentions_entity_snapshot_id", "entity_mentions", ["entity_snapshot_id"]
    )

    op.create_table(
        "source_attributions",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column("scan_analysis_id", UUIDType, nullable=False),
        sa.Column("response_source_id", UUIDType, nullable=False),
        sa.Column("entity_snapshot_id", UUIDType, nullable=False),
        sa.Column("source_host", sa.String(255), nullable=False),
        sa.Column("attribution_type", sa.String(30), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["scan_analysis_id"],
            ["scan_analyses.id"],
            ondelete="RESTRICT",
            name="fk_source_attributions_scan_analysis_id",
        ),
        sa.ForeignKeyConstraint(
            ["response_source_id"],
            ["response_sources.id"],
            ondelete="RESTRICT",
            name="fk_source_attributions_response_source_id",
        ),
        sa.ForeignKeyConstraint(
            ["entity_snapshot_id"],
            ["scan_entity_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_source_attributions_entity_snapshot_id",
        ),
        sa.UniqueConstraint(
            "scan_analysis_id",
            "response_source_id",
            "entity_snapshot_id",
            name="uq_source_attributions_analysis_source_entity",
        ),
    )
    op.create_index(
        "ix_source_attributions_scan_analysis_id",
        "source_attributions",
        ["scan_analysis_id"],
    )
    op.create_index(
        "ix_source_attributions_response_source_id",
        "source_attributions",
        ["response_source_id"],
    )
    op.create_index(
        "ix_source_attributions_entity_snapshot_id",
        "source_attributions",
        ["entity_snapshot_id"],
    )


def downgrade() -> None:
    op.drop_table("source_attributions")
    op.drop_table("entity_mentions")
    op.drop_table("scan_analyses")
    op.drop_table("scan_entity_snapshots")
