"""add occurrence action_engine_version_at_detection

Revision ID: b72aec534f9c
Revises: f9a1b2c3d4e5
Create Date: 2026-08-20 22:00:00.000000

Phase 9.1 — Action Center Concurrency, Citation Sufficiency and
Metric Consistency.

Schema changes:
- opportunity_occurrences.action_engine_version_at_detection: records
  the Action Engine methodology version used when this occurrence was
  detected. Historical occurrences from v1 retain "deterministic-actions-v1"
  via server_default. New occurrences from v1.1 refreshes record
  "deterministic-actions-v1.1".

This enables reliable historical interpretation of occurrence-level
methodology provenance without mutating historical records.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b72aec534f9c"
down_revision: str | Sequence[str] | None = "f9a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "opportunity_occurrences",
        sa.Column(
            "action_engine_version_at_detection",
            sa.String(50),
            nullable=False,
            server_default="deterministic-actions-v1",
        ),
    )


def downgrade() -> None:
    op.drop_column("opportunity_occurrences", "action_engine_version_at_detection")
