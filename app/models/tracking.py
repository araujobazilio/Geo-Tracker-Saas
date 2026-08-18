"""Project-related child models: keyword, competitor, provider, prompt."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import CompetitorSource, FunnelStage, LLMProvider, PromptType
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import StringList, UUIDType

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.prompt_set import PromptSet


class ProjectKeyword(UUIDPrimaryKey, TimestampMixin, Base):
    """A tracked topic / buyer intent for a project.

    `text` is the display form (trimmed, collapsed whitespace).
    `normalized_text` is the lowercased form used for uniqueness.

    Once created, keyword text is immutable. To change a keyword,
    deactivate the old one and create a new one. This preserves
    historical Prompt relationships.
    """

    __tablename__ = "project_keywords"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "normalized_text", name="uq_project_keyword_project_normalized_text"
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    text: Mapped[str] = mapped_column(String(500), nullable=False)
    normalized_text: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    intent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    funnel_stage: Mapped[FunnelStage | None] = mapped_column(String(20), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    project: Mapped[Project] = relationship(back_populates="keywords")
    prompts: Mapped[list[Prompt]] = relationship(
        back_populates="keyword",
        # Do NOT cascade-delete prompts when a keyword is deleted.
        # Historical prompts must survive. Keywords are deactivated,
        # not hard-deleted, in normal application flows.
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ProjectKeyword project={self.project_id} text={self.text!r}>"


class Competitor(UUIDPrimaryKey, TimestampMixin, Base):
    """A competitor tracked against a project's brand.

    Competitor domain is immutable after creation. To change domain,
    deactivate the old competitor and create a new one.
    """

    __tablename__ = "competitors"
    __table_args__ = (
        UniqueConstraint("project_id", "domain", name="uq_competitor_project_domain"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
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

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[LLMProvider] = mapped_column(String(30), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    project: Mapped[Project] = relationship(back_populates="providers")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ProjectProvider project={self.project_id} provider={self.provider}>"


class Prompt(UUIDPrimaryKey, Base):
    """A generated AI-search prompt linked to a keyword and prompt set.

    Prompt text is immutable once created. Regeneration creates a NEW
    PromptSet with new Prompt rows rather than overwriting existing ones,
    so that historical scans remain linked to the exact prompts used at
    the time.

    Note: `created_at` only (no `updated_at`) — prompts are immutable.

    `project_keyword_id` uses ON DELETE RESTRICT to prevent deletion of
    a keyword that has historical prompts. Keywords are deactivated
    instead of hard-deleted in normal application flows.
    """

    __tablename__ = "prompts"
    __table_args__ = (
        UniqueConstraint(
            "prompt_set_id",
            "project_keyword_id",
            "variant_index",
            name="uq_prompts_set_keyword_variant",
        ),
        CheckConstraint("variant_index > 0", name="ck_prompts_variant_index_positive"),
    )

    prompt_set_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("prompt_sets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    project_keyword_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("project_keywords.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(String(1000), nullable=False)
    prompt_type: Mapped[PromptType] = mapped_column(String(20), nullable=False, index=True)
    intent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    funnel_stage: Mapped[FunnelStage | None] = mapped_column(String(20), nullable=True)
    persona: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_country: Mapped[str | None] = mapped_column(String(10), nullable=True)
    target_language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    commercial_intent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    keyword: Mapped[ProjectKeyword] = relationship(back_populates="prompts")
    prompt_set: Mapped[PromptSet] = relationship(back_populates="prompts")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Prompt id={self.id} type={self.prompt_type} variant={self.variant_index}>"
