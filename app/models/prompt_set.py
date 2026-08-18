"""PromptSet model — a complete immutable set of prompts for a project.

A PromptSet represents one complete generation of prompts for a project.
Each PromptSet is immutable once created. When project configuration
changes, a new PromptSet version is generated rather than overwriting
the old one.

Lifecycle:
  ACTIVE    → the current prompt set used by scans
  SUPERSEDED → replaced by a newer ACTIVE set

There is at most one ACTIVE PromptSet per project (enforced by a
partial unique index).

The `input_revision` field records the Project.prompt_input_revision
at generation time. A PromptSet is "fresh" when its input_revision
matches the project's current prompt_input_revision, and "stale"
otherwise.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import PromptSetStatus
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.tracking import Prompt


class PromptSet(UUIDPrimaryKey, TimestampMixin, Base):
    """A complete immutable set of prompts for a project.

    Constraints:
      - version > 0
      - input_revision > 0
      - unique (project_id, version)
      - at most one ACTIVE per project (partial unique index)
    """

    __tablename__ = "prompt_sets"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_prompt_sets_project_version"),
        CheckConstraint("version > 0", name="ck_prompt_sets_version_positive"),
        CheckConstraint("input_revision > 0", name="ck_prompt_sets_input_revision_positive"),
        # Partial unique index: at most one ACTIVE prompt set per project.
        Index(
            "uq_prompt_sets_one_active_per_project",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PromptSetStatus] = mapped_column(
        String(20), nullable=False, default=PromptSetStatus.ACTIVE, index=True
    )
    generator_key: Mapped[str] = mapped_column(String(100), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship(back_populates="prompt_sets")
    prompts: Mapped[list[Prompt]] = relationship(
        back_populates="prompt_set", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<PromptSet project={self.project_id} " f"version={self.version} status={self.status}>"
        )
