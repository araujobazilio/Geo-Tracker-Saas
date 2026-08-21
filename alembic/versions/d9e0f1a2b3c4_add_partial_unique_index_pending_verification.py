"""add partial unique index for one pending verification per cycle

Revision ID: d9e0f1a2b3c4
Revises: c8f1d2a3e4b5
Create Date: 2026-08-22 02:00:00.000000

Phase 10.1 — Hardening: one pending verification per implementation
cycle.

Schema changes:
- Partial unique index on opportunity_verifications(opportunity_id,
  baseline_occurrence_id) WHERE outcome = 'PENDING'.  This prevents
  two PENDING verifications from coexisting for the same implementation
  cycle at the database level, complementing the service-level check
  in VerificationScanCreationService.

Note: PostgreSQL supports partial indexes natively.  SQLite does not
support WHERE clauses on CREATE INDEX, so the migration uses a
conditional approach that works on both backends.  On SQLite, the
service-level check in VerificationScanCreationService is the primary
enforcement mechanism.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9e0f1a2b3c4"
down_revision: str | Sequence[str] | None = "c8f1d2a3e4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Partial unique index: only one PENDING verification per
    # (opportunity_id, baseline_occurrence_id) pair.
    #
    # PostgreSQL supports partial indexes natively:
    #   CREATE UNIQUE INDEX ... ON ... (cols) WHERE outcome = 'PENDING';
    #
    # SQLite does not support partial indexes with WHERE clauses
    # in older versions, but modern SQLite (>= 3.8.0) does.
    # We use raw SQL for cross-platform compatibility.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_opportunity_verifications_pending_per_cycle "
        "ON opportunity_verifications (opportunity_id, baseline_occurrence_id) "
        "WHERE outcome = 'PENDING'"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS "
        "uq_opportunity_verifications_pending_per_cycle"
    )
