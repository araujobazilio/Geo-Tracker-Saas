"""Phase 7 deterministic analysis models.

These models store immutable entity snapshots, analysis runs, detected
mentions, and source attributions. They are derived evidence — Phase 7
never modifies original PromptRun or ResponseSource rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    AttributionType,
    EntityMatchType,
    ScanAnalysisStatus,
    TrackedEntityType,
)
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import StringList, UUIDType

if TYPE_CHECKING:
    from app.models.scan import PromptRun, ResponseSource, Scan


ANALYSIS_VERSION = "deterministic-entity-v1"


class ScanEntitySnapshot(UUIDPrimaryKey, TimestampMixin, Base):
    """Immutable snapshot of a tracked entity (brand or competitor) at Scan
    creation time.

    Historical metrics use ONLY these snapshots, never the mutable
    Project/Competitor rows, so that post-scan configuration changes
    cannot rewrite historical analysis.
    """

    __tablename__ = "scan_entity_snapshots"
    __table_args__ = (
        UniqueConstraint("scan_id", "entity_key", name="uq_scan_entity_snapshots_scan_entity_key"),
        CheckConstraint("ordinal > 0", name="ck_scan_entity_snapshots_ordinal_positive"),
        CheckConstraint("name <> ''", name="ck_scan_entity_snapshots_name_non_empty"),
        CheckConstraint("domain <> ''", name="ck_scan_entity_snapshots_domain_non_empty"),
    )

    scan_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("scans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    entity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[TrackedEntityType] = mapped_column(String(20), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(StringList, nullable=False, default=list)
    source_competitor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("competitors.id", ondelete="SET NULL"),
        nullable=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    scan: Mapped[Scan] = relationship()


class ScanAnalysis(UUIDPrimaryKey, TimestampMixin, Base):
    """One deterministic analysis run for a Scan at a specific algorithm
    version.

    Unique on (scan_id, analysis_version). A COMPLETED analysis is
    immutable — future algorithm versions create a new ScanAnalysis row.
    """

    __tablename__ = "scan_analyses"
    __table_args__ = (
        UniqueConstraint("scan_id", "analysis_version", name="uq_scan_analyses_scan_version"),
        CheckConstraint("warning_count >= 0", name="ck_scan_analyses_warning_count_non_negative"),
    )

    scan_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("scans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    analysis_version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[ScanAnalysisStatus] = mapped_column(
        String(20), nullable=False, default=ScanAnalysisStatus.PENDING, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    scan: Mapped[Scan] = relationship()
    mentions: Mapped[list[EntityMention]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )
    attributions: Mapped[list[SourceAttribution]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )


class EntityMention(UUIDPrimaryKey, Base):
    """One occurrence of a tracked entity term in a SUCCEEDED PromptRun
    response text.

    Occurrences are ordered by their position in the original response.
    Overlapping terms for the same entity are deduplicated to the longest
    match. Ambiguous terms shared by multiple entities are excluded.
    """

    __tablename__ = "entity_mentions"
    __table_args__ = (
        UniqueConstraint(
            "scan_analysis_id",
            "prompt_run_id",
            "entity_snapshot_id",
            "occurrence_index",
            name="uq_entity_mentions_analysis_run_entity_occurrence",
        ),
        CheckConstraint("occurrence_index > 0", name="ck_entity_mentions_occurrence_positive"),
        CheckConstraint("start_index >= 0", name="ck_entity_mentions_start_non_negative"),
        CheckConstraint("end_index > start_index", name="ck_entity_mentions_end_gt_start"),
    )

    scan_analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("scan_analyses.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    prompt_run_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("prompt_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    entity_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("scan_entity_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    occurrence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    match_type: Mapped[EntityMatchType] = mapped_column(String(20), nullable=False)
    matched_text: Mapped[str] = mapped_column(Text, nullable=False)
    matched_term: Mapped[str] = mapped_column(String(255), nullable=False)
    start_index: Mapped[int] = mapped_column(Integer, nullable=False)
    end_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    analysis: Mapped[ScanAnalysis] = relationship(back_populates="mentions")
    prompt_run: Mapped[PromptRun] = relationship()
    entity_snapshot: Mapped[ScanEntitySnapshot] = relationship()


class SourceAttribution(UUIDPrimaryKey, Base):
    """Attribution of a ResponseSource to a tracked entity via domain
    matching.

    Only OWNED_DOMAIN attributions are created in Phase 7: the source
    hostname matches the entity's tracked domain exactly or as a
    subdomain. Most-specific domain wins when multiple tracked domains
    could match.
    """

    __tablename__ = "source_attributions"
    __table_args__ = (
        UniqueConstraint(
            "scan_analysis_id",
            "response_source_id",
            "entity_snapshot_id",
            name="uq_source_attributions_analysis_source_entity",
        ),
    )

    scan_analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("scan_analyses.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    response_source_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("response_sources.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    entity_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("scan_entity_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_host: Mapped[str] = mapped_column(String(255), nullable=False)
    attribution_type: Mapped[AttributionType] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    analysis: Mapped[ScanAnalysis] = relationship(back_populates="attributions")
    response_source: Mapped[ResponseSource] = relationship()
    entity_snapshot: Mapped[ScanEntitySnapshot] = relationship()
