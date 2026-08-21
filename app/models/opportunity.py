"""Phase 9 Action Center models.

Opportunities are logical, deduplicated workflow entities representing
evidence-based gaps detected from deterministic Scan analysis. Each
Opportunity has a stable fingerprint for cross-scan deduplication, and
accumulates immutable Occurrence records (one per detecting Scan) with
typed Evidence rows.

Key invariants:
- Opportunities are never hard-deleted.
- Status is preserved across refreshes (OPEN/IN_PROGRESS/IMPLEMENTED/
  DISMISSED/VERIFIED).
- Occurrences and Evidence are immutable once written.
- All evidence is traceable to persisted Scan evidence
  (PromptRun, EntityMention, SourceAttribution, ScanEntitySnapshot).
- Zero AI Checks, zero provider calls, zero UsageEvents.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    LLMProvider,
    OpportunityEvidenceType,
    OpportunityPriority,
    OpportunityStatus,
    OpportunityType,
    PromptType,
)
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.workspace import Workspace


class Opportunity(UUIDPrimaryKey, TimestampMixin, Base):
    """A logical, deduplicated evidence-based gap the user can act on.

    Identified by a stable fingerprint (project_id, fingerprint) so the
    same logical issue across multiple Scans updates the same row rather
    than creating duplicates. Human workflow status is preserved across
    automated refreshes.
    """

    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("project_id", "fingerprint", name="uq_opportunities_project_fingerprint"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    opportunity_type: Mapped[OpportunityType] = mapped_column(
        String(50), nullable=False, index=True
    )
    status: Mapped[OpportunityStatus] = mapped_column(
        String(20), nullable=False, index=True, default=OpportunityStatus.OPEN
    )
    priority: Mapped[OpportunityPriority] = mapped_column(String(10), nullable=False, index=True)
    action_engine_version: Mapped[str] = mapped_column(String(50), nullable=False)

    competitor_entity_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[LLMProvider | None] = mapped_column(String(20), nullable=True, index=True)
    prompt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("prompts.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    prompt_type: Mapped[PromptType] = mapped_column(String(20), nullable=False)

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)

    first_detected_scan_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("scans.id", ondelete="RESTRICT"), nullable=False
    )
    latest_detected_scan_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("scans.id", ondelete="RESTRICT"), nullable=False
    )

    first_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    implemented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    occurrences: Mapped[list[OpportunityOccurrence]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )

    # Type-ignore for SQLAlchemy typed relationship resolution
    workspace: Mapped[Workspace] = relationship()
    project: Mapped[Project] = relationship()


class OpportunityOccurrence(UUIDPrimaryKey, Base):
    """One immutable record: 'This Opportunity was observed in Scan X.'

    A new Occurrence is created each time a different Scan detects the
    same logical Opportunity. Occurrences are never rewritten.
    """

    __tablename__ = "opportunity_occurrences"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "scan_id", name="uq_opportunity_occurrences_opp_scan"),
    )

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("opportunities.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scan_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("scans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    scan_analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("scan_analyses.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    competitor_entity_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("scan_entity_snapshots.id", ondelete="RESTRICT"),
        nullable=True,
    )
    brand_entity_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("scan_entity_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
    )

    priority_at_detection: Mapped[OpportunityPriority] = mapped_column(String(10), nullable=False)
    action_engine_version_at_detection: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="deterministic-actions-v1"
    )

    brand_visibility: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    competitor_visibility: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    visibility_gap_pp: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)

    brand_citation_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    competitor_citation_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    citation_gap_pp: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)

    measurement_coverage: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    opportunity: Mapped[Opportunity] = relationship(back_populates="occurrences")
    evidence: Mapped[list[OpportunityEvidence]] = relationship(
        back_populates="occurrence", cascade="all, delete-orphan"
    )


class OpportunityEvidence(UUIDPrimaryKey, Base):
    """Typed evidence row backing an Opportunity Occurrence.

    Each evidence row links to specific persisted Scan evidence
    (PromptRun, ResponseSource, metric gap) so every Opportunity can be
    traced back to immutable measurement data.
    """

    __tablename__ = "opportunity_evidence"
    __table_args__ = (
        UniqueConstraint("occurrence_id", "evidence_key", name="uq_opportunity_evidence_occ_key"),
    )

    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("opportunity_occurrences.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    evidence_key: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_type: Mapped[OpportunityEvidenceType] = mapped_column(String(30), nullable=False)

    prompt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("prompts.id", ondelete="RESTRICT"), nullable=True
    )
    prompt_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("prompt_runs.id", ondelete="RESTRICT"), nullable=True
    )
    response_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("response_sources.id", ondelete="RESTRICT"), nullable=True
    )

    provider: Mapped[LLMProvider | None] = mapped_column(String(20), nullable=True)

    metric_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    brand_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    competitor_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    delta_value: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    occurrence: Mapped[OpportunityOccurrence] = relationship(back_populates="evidence")
