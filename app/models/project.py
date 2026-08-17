"""Project model."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ProjectStatus
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import StringList, UUIDType

if TYPE_CHECKING:
    from app.models.tracking import Competitor, ProjectKeyword, ProjectProvider


class Project(UUIDPrimaryKey, TimestampMixin, Base):
    """A tracked website / brand belonging to a Workspace.

    `brand_aliases` is stored as a JSONB array of strings. This is a
    deliberate trade-off: aliases are a small, schemaless, read-mostly
    collection that does not justify a full child table, while still
    preserving structure (vs. comma-separated text). See
    `docs/DATABASE.md` for the rationale.
    """

    __tablename__ = "projects"

    workspace_id: Mapped[str] = mapped_column(
        UUIDType,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    brand_name: Mapped[str] = mapped_column(String(255), nullable=False)
    brand_aliases: Mapped[list[str]] = mapped_column(StringList, nullable=False, default=list)
    industry: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_country: Mapped[str | None] = mapped_column(String(10), nullable=True)
    target_language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    target_audience: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        String(20), nullable=False, default=ProjectStatus.ACTIVE, index=True
    )
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    keywords: Mapped[list[ProjectKeyword]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    competitors: Mapped[list[Competitor]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    providers: Mapped[list[ProjectProvider]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Project id={self.id} name={self.name!r} domain={self.domain!r}>"
