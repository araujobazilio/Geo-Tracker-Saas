"""add confidence scan support

Revision ID: e7f8a9b0c1d2
Revises: d677b6e44e9b
Create Date: 2026-08-20 10:00:00.000000

Phase 8 — Confidence Scans.

Schema changes:
- scans.repeat_count: positive integer, default 1 (STANDARD=1, CONFIDENCE>=2)
- scans.baseline_scan_id: self-referencing FK to scans.id (ON DELETE RESTRICT)
- prompt_runs.observation_index: positive integer, default 1
- Drop old unique (scan_id, prompt_id, provider, attempt_number)
- Add unique (scan_id, prompt_id, provider, observation_index, attempt_number)
- Add check constraints: repeat_count > 0, observation_index > 0
- Add index (scan_id, observation_index)

Existing rows are backfilled: repeat_count=1, observation_index=1.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.types import UUIDType

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "d677b6e44e9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- scans: repeat_count ---
    op.add_column(
        "scans",
        sa.Column(
            "repeat_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_check_constraint(
        "ck_scans_repeat_count_positive",
        "scans",
        "repeat_count > 0",
    )

    # --- scans: baseline_scan_id (self-referencing FK) ---
    op.add_column(
        "scans",
        sa.Column("baseline_scan_id", UUIDType, nullable=True),
    )
    op.create_index(
        "ix_scans_baseline_scan_id",
        "scans",
        ["baseline_scan_id"],
    )
    op.create_foreign_key(
        "fk_scans_baseline_scan_id_scans",
        "scans",
        "scans",
        ["baseline_scan_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- prompt_runs: observation_index ---
    op.add_column(
        "prompt_runs",
        sa.Column(
            "observation_index",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_check_constraint(
        "ck_prompt_runs_observation_positive",
        "prompt_runs",
        "observation_index > 0",
    )
    op.create_index(
        "ix_prompt_runs_observation_index",
        "prompt_runs",
        ["observation_index"],
    )
    op.create_index(
        "ix_prompt_runs_scan_observation",
        "prompt_runs",
        ["scan_id", "observation_index"],
    )

    # --- Replace unique constraint ---
    op.drop_constraint(
        "uq_prompt_runs_scan_prompt_provider_attempt",
        "prompt_runs",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_prompt_runs_scan_prompt_provider_obs_attempt",
        "prompt_runs",
        [
            "scan_id",
            "prompt_id",
            "provider",
            "observation_index",
            "attempt_number",
        ],
    )


def downgrade() -> None:
    # --- Revert unique constraint ---
    op.drop_constraint(
        "uq_prompt_runs_scan_prompt_provider_obs_attempt",
        "prompt_runs",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_prompt_runs_scan_prompt_provider_attempt",
        "prompt_runs",
        ["scan_id", "prompt_id", "provider", "attempt_number"],
    )

    # --- prompt_runs: observation_index ---
    op.drop_index("ix_prompt_runs_scan_observation", table_name="prompt_runs")
    op.drop_index("ix_prompt_runs_observation_index", table_name="prompt_runs")
    op.drop_constraint("ck_prompt_runs_observation_positive", "prompt_runs", type_="check")
    op.drop_column("prompt_runs", "observation_index")

    # --- scans: baseline_scan_id ---
    op.drop_constraint("fk_scans_baseline_scan_id_scans", "scans", type_="foreignkey")
    op.drop_index("ix_scans_baseline_scan_id", table_name="scans")
    op.drop_column("scans", "baseline_scan_id")

    # --- scans: repeat_count ---
    op.drop_constraint("ck_scans_repeat_count_positive", "scans", type_="check")
    op.drop_column("scans", "repeat_count")
