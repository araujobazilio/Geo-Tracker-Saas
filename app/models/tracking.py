"""Project-related child models: keyword, competitor, provider, prompt."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import CompetitorSource, FunnelStage, LLMProvider, PromptType
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import StringList, UUIDType

if TYPE_CHECKING:
    from app.models.project import Project


class ProjectKeyword(UUIDPrimaryKey, TimestampMixin, Base):
    """A tracked topic / buyer intent for a project."""

    __tablename__ = "project_keywords"
    __table_args__ = (
        UniqueConstraint("project_id", "text", name="uq_project_keyword_project_text"),
    )

    project_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(String(500), nullable=False)
    intent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    funnel_stage: Mapped[FunnelStage | None] = mapped_column(String(20), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    project: Mapped[Project] = relationship(back_populates="keywords")
    prompts: Mapped[list[Prompt]] = relationship(
        back_populates="keyword", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ProjectKeyword project={self.project_id} text={self.text!r}>"


class Competitor(UUIDPrimaryKey, TimestampMixin, Base):
    """A competitor tracked against a project's brand."""

    __tablename__ = "competitors"
    __table_args__ = (
        UniqueConstraint("project_id", "domain", name="uq_competitor_project_domain"),
    )

    project_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(StringList, nullable=False, default=list)
    source: Mapped[CompetitorSource] = mapped_column(
        String(20), nullable=False, default=CompetitorSource.USER_DEFINED
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    project: Mapped[Project] = relationship(back_populates="competitors")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Competitor project={self.project_id} name={self.name!r}>"


class ProjectProvider(UUIDPrimaryKey, TimestampMixin, Base):
    """Per-project enabled AI provider configuration."""

    __tablename__ = "project_providers"
    __table_args__ = (
        UniqueConstraint("project_id", "provider", name="uq_project_provider_project_provider"),
    )

    project_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[LLMProvider] = mapped_column(String(30), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    project: Mapped[Project] = relationship(back_populates="providers")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ProjectProvider project={self.project_id} provider={self.provider}>"


class Prompt(UUIDPrimaryKey, Base):
    """A generated AI-search prompt linked to a keyword.

    Prompt text is versioned via `prompt_set_version`. Regeneration must
    create a NEW version rather than overwriting existing rows, so that
    historical scans remain linked to the exact prompts used at the time.

    Note: `created_at` only (no `updated_at`) — prompts are immutable once
    created; edits produce a new version.
    """

    __tablename__ = "prompts"

    project_keyword_id: Mapped[str] = mapped_column(
        UUIDType,
        ForeignKey("project_keywords.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(String(1000), nullable=False)
    prompt_set_version: Mapped[int] = mapped_column(nullable=False, default=1, index=True)
    prompt_type: Mapped[PromptType] = mapped_column(String(20), nullable=False, index=True)
    intent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    funnel_stage: Mapped[FunnelStage | None] = mapped_column(String(20), nullable=True)
    persona: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_country: Mapped[str | None] = mapped_column(String(10), nullable=True)
    target_language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    commercial_intent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    keyword: Mapped[ProjectKeyword] = relationship(back_populates="prompts")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Prompt id={self.id} type={self.prompt_type} v{self.prompt_set_version}>"
