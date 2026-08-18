"""add project onboarding and versioned prompt sets

Phase 4 — Project Onboarding and Versioned Prompt System.

Changes:
- projects: add prompt_input_revision (Integer NOT NULL default 1, CHECK > 0)
- prompt_sets: new table for immutable versioned prompt sets
- project_keywords: add normalized_text, replace unique constraint
  from (project_id, text) to (project_id, normalized_text)
- prompts: add prompt_set_id FK, variant_index, change project_keyword_id
  FK from CASCADE to RESTRICT, backfill prompt_set_id from existing
  prompt_set_version, then drop prompt_set_version

Backfill strategy:
  - For existing prompts, group by (project_keyword's project_id, prompt_set_version)
    and create corresponding PromptSet records.
  - Maximum existing version per project becomes ACTIVE; older become SUPERSEDED.
  - Generate deterministic variant_index values for legacy rows.
  - Backfill normalized_text for existing keywords from text.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-08-19 10:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.db.types import UUIDType

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- projects: add prompt_input_revision ---
    op.add_column(
        "projects",
        sa.Column("prompt_input_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_projects_prompt_input_revision_positive",
        "projects",
        "prompt_input_revision > 0",
    )

    # --- prompt_sets: new table ---
    op.create_table(
        "prompt_sets",
        sa.Column("id", UUIDType, primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            UUIDType,
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE", index=True),
        sa.Column("generator_key", sa.String(100), nullable=False),
        sa.Column(
            "created_by_user_id",
            UUIDType,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
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
            "project_id", "version", name="uq_prompt_sets_project_version"
        ),
        sa.CheckConstraint("version > 0", name="ck_prompt_sets_version_positive"),
        sa.CheckConstraint(
            "input_revision > 0", name="ck_prompt_sets_input_revision_positive"
        ),
    )
    # Partial unique index: at most one ACTIVE prompt set per project.
    op.execute(
        "CREATE UNIQUE INDEX uq_prompt_sets_one_active_per_project "
        "ON prompt_sets (project_id) WHERE status = 'ACTIVE'"
    )

    # --- project_keywords: add normalized_text ---
    op.add_column(
        "project_keywords",
        sa.Column("normalized_text", sa.String(500), nullable=True),
    )
    # Backfill normalized_text from text (lowercase, trimmed).
    op.execute(
        "UPDATE project_keywords SET normalized_text = LOWER(TRIM(text)) "
        "WHERE normalized_text IS NULL"
    )
    # Now make it NOT NULL.
    op.alter_column(
        "project_keywords",
        "normalized_text",
        existing_type=sa.String(500),
        nullable=False,
    )
    # Create index on normalized_text.
    op.create_index(
        "ix_project_keywords_normalized_text",
        "project_keywords",
        ["normalized_text"],
    )
    # Drop old unique constraint on (project_id, text) and add new one.
    op.drop_constraint(
        "uq_project_keyword_project_text", "project_keywords", type_="unique"
    )
    op.create_unique_constraint(
        "uq_project_keyword_project_normalized_text",
        "project_keywords",
        ["project_id", "normalized_text"],
    )

    # --- prompts: add prompt_set_id, variant_index, created_at ---
    op.add_column(
        "prompts",
        sa.Column("prompt_set_id", UUIDType, nullable=True),
    )
    op.add_column(
        "prompts",
        sa.Column("variant_index", sa.Integer(), nullable=True),
    )
    op.add_column(
        "prompts",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Backfill: create PromptSet records from existing prompts.
    # Group by (project_id, prompt_set_version) and create a PromptSet for each.
    op.execute(
        """
        INSERT INTO prompt_sets (id, project_id, version, input_revision, status, generator_key, created_at, updated_at)
        SELECT
            gen_random_uuid(),
            pk.project_id,
            p.prompt_set_version,
            MAX(proj.prompt_input_revision),
            CASE
                WHEN p.prompt_set_version = (
                    SELECT MAX(p2.prompt_set_version)
                    FROM prompts p2
                    JOIN project_keywords pk2 ON p2.project_keyword_id = pk2.id
                    WHERE pk2.project_id = pk.project_id
                ) THEN 'ACTIVE'
                ELSE 'SUPERSEDED'
            END,
            'deterministic-template-v1',
            NOW(),
            NOW()
        FROM prompts p
        JOIN project_keywords pk ON p.project_keyword_id = pk.id
        JOIN projects proj ON pk.project_id = proj.id
        GROUP BY pk.project_id, p.prompt_set_version
        """
    )

    # Backfill prompt_set_id by matching (project_id, prompt_set_version).
    op.execute(
        """
        UPDATE prompts p
        SET prompt_set_id = (
            SELECT ps.id
            FROM prompt_sets ps
            JOIN project_keywords pk ON p.project_keyword_id = pk.id
            WHERE ps.project_id = pk.project_id
              AND ps.version = p.prompt_set_version
        )
        WHERE p.prompt_set_id IS NULL
        """
    )

    # Backfill variant_index: deterministic per (prompt_set_id, project_keyword_id).
    op.execute(
        """
        UPDATE prompts p
        SET variant_index = sub.rn
        FROM (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY prompt_set_id, project_keyword_id
                    ORDER BY id
                ) AS rn
            FROM prompts
        ) sub
        WHERE p.id = sub.id AND p.variant_index IS NULL
        """
    )

    # Verify backfill completed.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM prompts WHERE prompt_set_id IS NULL) THEN
                RAISE EXCEPTION
                    'Cannot make prompt_set_id NOT NULL: % prompts could not be matched to a prompt set',
                    (SELECT count(*) FROM prompts WHERE prompt_set_id IS NULL);
            END IF;
            IF EXISTS (SELECT 1 FROM prompts WHERE variant_index IS NULL) THEN
                RAISE EXCEPTION
                    'Cannot make variant_index NOT NULL: % prompts have no variant_index',
                    (SELECT count(*) FROM prompts WHERE variant_index IS NULL);
            END IF;
        END $$;
        """
    )

    # Add FK for prompt_set_id.
    op.create_foreign_key(
        "fk_prompts_prompt_set_id",
        "prompts",
        "prompt_sets",
        ["prompt_set_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Make columns NOT NULL.
    op.alter_column("prompts", "prompt_set_id", existing_type=UUIDType, nullable=False)
    op.alter_column("prompts", "variant_index", existing_type=sa.Integer(), nullable=False)
    op.create_index("ix_prompts_prompt_set_id", "prompts", ["prompt_set_id"])

    # Add check constraint for variant_index > 0.
    op.create_check_constraint(
        "ck_prompts_variant_index_positive", "prompts", "variant_index > 0"
    )

    # Add unique constraint for (prompt_set_id, project_keyword_id, variant_index).
    op.create_unique_constraint(
        "uq_prompts_set_keyword_variant",
        "prompts",
        ["prompt_set_id", "project_keyword_id", "variant_index"],
    )

    # Change project_keyword_id FK from CASCADE to RESTRICT.
    op.drop_constraint(
        "prompts_project_keyword_id_fkey", "prompts", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_prompts_project_keyword_id",
        "prompts",
        "project_keywords",
        ["project_keyword_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Drop prompt_set_version column.
    op.drop_column("prompts", "prompt_set_version")


def downgrade() -> None:
    # Restore prompt_set_version from prompt_sets.version.
    op.add_column(
        "prompts",
        sa.Column("prompt_set_version", sa.Integer(), nullable=True),
    )
    op.execute(
        """
        UPDATE prompts p
        SET prompt_set_version = ps.version
        FROM prompt_sets ps
        WHERE p.prompt_set_id = ps.id
        """
    )
    op.alter_column("prompts", "prompt_set_version", existing_type=sa.Integer(), nullable=False)
    op.create_index("ix_prompts_prompt_set_version", "prompts", ["prompt_set_version"])

    # Restore project_keyword_id FK to CASCADE.
    op.drop_constraint("fk_prompts_project_keyword_id", "prompts", type_="foreignkey")
    op.create_foreign_key(
        "prompts_project_keyword_id_fkey",
        "prompts",
        "project_keywords",
        ["project_keyword_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Drop prompts constraints and columns.
    op.drop_constraint("uq_prompts_set_keyword_variant", "prompts", type_="unique")
    op.drop_constraint("ck_prompts_variant_index_positive", "prompts", type_="check")
    op.drop_index("ix_prompts_prompt_set_id", table_name="prompts")
    op.alter_column("prompts", "variant_index", existing_type=sa.Integer(), nullable=True)
    op.alter_column("prompts", "prompt_set_id", existing_type=UUIDType, nullable=True)
    op.drop_constraint("fk_prompts_prompt_set_id", "prompts", type_="foreignkey")
    op.drop_column("prompts", "variant_index")
    op.drop_column("prompts", "prompt_set_id")
    op.drop_column("prompts", "created_at")

    # Restore project_keywords unique constraint.
    op.drop_constraint(
        "uq_project_keyword_project_normalized_text", "project_keywords", type_="unique"
    )
    op.drop_index("ix_project_keywords_normalized_text", table_name="project_keywords")
    op.create_unique_constraint(
        "uq_project_keyword_project_text", "project_keywords", ["project_id", "text"]
    )
    op.drop_column("project_keywords", "normalized_text")

    # Drop prompt_sets table.
    op.execute("DROP INDEX IF EXISTS uq_prompt_sets_one_active_per_project")
    op.drop_table("prompt_sets")

    # Drop projects.prompt_input_revision.
    op.drop_constraint(
        "ck_projects_prompt_input_revision_positive", "projects", type_="check"
    )
    op.drop_column("projects", "prompt_input_revision")
