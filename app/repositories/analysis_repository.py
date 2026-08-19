"""Repositories for Phase 7 analysis models."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.analysis import (
    EntityMention,
    ScanAnalysis,
    ScanEntitySnapshot,
    SourceAttribution,
)


class ScanEntitySnapshotRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_batch(self, snapshots: list[ScanEntitySnapshot]) -> list[ScanEntitySnapshot]:
        self._session.add_all(snapshots)
        self._session.flush()
        return snapshots

    def list_by_scan(self, scan_id: uuid.UUID) -> list[ScanEntitySnapshot]:
        return list(
            self._session.execute(
                select(ScanEntitySnapshot)
                .where(ScanEntitySnapshot.scan_id == scan_id)
                .order_by(ScanEntitySnapshot.ordinal)
            ).scalars()
        )

    def get_by_scan_and_key(self, scan_id: uuid.UUID, entity_key: str) -> ScanEntitySnapshot | None:
        return self._session.execute(
            select(ScanEntitySnapshot).where(
                ScanEntitySnapshot.scan_id == scan_id,
                ScanEntitySnapshot.entity_key == entity_key,
            )
        ).scalar_one_or_none()

    def count_by_scan(self, scan_id: uuid.UUID) -> int:
        return int(
            self._session.execute(
                select(func.count(ScanEntitySnapshot.id)).where(
                    ScanEntitySnapshot.scan_id == scan_id
                )
            ).scalar_one()
        )


class ScanAnalysisRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, analysis: ScanAnalysis) -> ScanAnalysis:
        self._session.add(analysis)
        self._session.flush()
        return analysis

    def get_by_id(self, analysis_id: uuid.UUID) -> ScanAnalysis | None:
        return self._session.get(ScanAnalysis, analysis_id)

    def get_for_update(self, scan_id: uuid.UUID, analysis_version: str) -> ScanAnalysis | None:
        return self._session.execute(
            select(ScanAnalysis)
            .where(
                ScanAnalysis.scan_id == scan_id,
                ScanAnalysis.analysis_version == analysis_version,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def get_by_scan_and_version(
        self, scan_id: uuid.UUID, analysis_version: str
    ) -> ScanAnalysis | None:
        return self._session.execute(
            select(ScanAnalysis).where(
                ScanAnalysis.scan_id == scan_id,
                ScanAnalysis.analysis_version == analysis_version,
            )
        ).scalar_one_or_none()

    def get_scoped(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scan_id: uuid.UUID,
        analysis_version: str | None = None,
    ) -> ScanAnalysis | None:
        from app.models.scan import Scan

        stmt = (
            select(ScanAnalysis)
            .join(Scan, ScanAnalysis.scan_id == Scan.id)
            .where(
                ScanAnalysis.scan_id == scan_id,
                Scan.workspace_id == workspace_id,
                Scan.project_id == project_id,
            )
        )
        if analysis_version is not None:
            stmt = stmt.where(ScanAnalysis.analysis_version == analysis_version)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_by_scan(self, scan_id: uuid.UUID) -> list[ScanAnalysis]:
        return list(
            self._session.execute(
                select(ScanAnalysis)
                .where(ScanAnalysis.scan_id == scan_id)
                .order_by(ScanAnalysis.created_at)
            ).scalars()
        )


class EntityMentionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_batch(self, mentions: list[EntityMention]) -> list[EntityMention]:
        self._session.add_all(mentions)
        self._session.flush()
        return mentions

    def list_by_analysis(self, analysis_id: uuid.UUID) -> list[EntityMention]:
        return list(
            self._session.execute(
                select(EntityMention)
                .where(EntityMention.scan_analysis_id == analysis_id)
                .order_by(EntityMention.prompt_run_id, EntityMention.occurrence_index)
            ).scalars()
        )

    def list_by_analysis_and_run(
        self, analysis_id: uuid.UUID, prompt_run_id: uuid.UUID
    ) -> list[EntityMention]:
        return list(
            self._session.execute(
                select(EntityMention)
                .where(
                    EntityMention.scan_analysis_id == analysis_id,
                    EntityMention.prompt_run_id == prompt_run_id,
                )
                .order_by(EntityMention.occurrence_index)
            ).scalars()
        )

    def delete_by_analysis(self, analysis_id: uuid.UUID) -> None:
        mentions = self.list_by_analysis(analysis_id)
        for mention in mentions:
            self._session.delete(mention)
        self._session.flush()


class SourceAttributionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_batch(self, attributions: list[SourceAttribution]) -> list[SourceAttribution]:
        self._session.add_all(attributions)
        self._session.flush()
        return attributions

    def list_by_analysis(self, analysis_id: uuid.UUID) -> list[SourceAttribution]:
        return list(
            self._session.execute(
                select(SourceAttribution)
                .where(SourceAttribution.scan_analysis_id == analysis_id)
                .order_by(SourceAttribution.response_source_id)
            ).scalars()
        )

    def list_by_analysis_and_source(
        self, analysis_id: uuid.UUID, response_source_id: uuid.UUID
    ) -> list[SourceAttribution]:
        return list(
            self._session.execute(
                select(SourceAttribution).where(
                    SourceAttribution.scan_analysis_id == analysis_id,
                    SourceAttribution.response_source_id == response_source_id,
                )
            ).scalars()
        )

    def delete_by_analysis(self, analysis_id: uuid.UUID) -> None:
        attributions = self.list_by_analysis(analysis_id)
        for attr in attributions:
            self._session.delete(attr)
        self._session.flush()
