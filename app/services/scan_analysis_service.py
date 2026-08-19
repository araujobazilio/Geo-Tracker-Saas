"""ScanAnalysisService — deterministic, zero-cost analysis of persisted evidence.

Analyzes SUCCEEDED PromptRun response text and ResponseSource URLs
against immutable ScanEntitySnapshots. Produces EntityMention and
SourceAttribution evidence rows atomically.

Key invariants:
- Zero provider API calls, zero AI Checks, zero UsageEvents.
- Idempotent: re-analyzing the same (scan_id, analysis_version) returns
  the existing COMPLETED analysis without duplicating evidence.
- Concurrent-safe: row locking + unique constraints prevent duplicates.
- FAILED analysis can be safely retried (evidence from the failed run
  is deleted before re-inserting).
- Only terminal scans with at least one SUCCEEDED run are eligible.
- Scans without entity snapshots fail with MISSING_ENTITY_SNAPSHOT.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import (
    AttributionType,
    PromptRunStatus,
    ScanAnalysisStatus,
    ScanStatus,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.analysis import (
    ANALYSIS_VERSION,
    EntityMention,
    ScanAnalysis,
    SourceAttribution,
)
from app.models.scan import PromptRun, Scan
from app.repositories.analysis_repository import (
    EntityMentionRepository,
    ScanAnalysisRepository,
    ScanEntitySnapshotRepository,
)
from app.repositories.scan_repository import PromptRunRepository, ScanRepository
from app.services.detection.mention_detector import (
    WarningCode,
    build_entity_terms,
    detect_mentions,
)
from app.services.detection.source_attributor import (
    attribute_source,
    build_entity_domains,
    parse_source_host,
)

logger = get_logger("app.scan_analysis")


class ScanAnalysisService:
    """Orchestrate deterministic analysis of a terminal Scan."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._scans = ScanRepository(session)
        self._runs = PromptRunRepository(session)
        self._snapshots = ScanEntitySnapshotRepository(session)
        self._analyses = ScanAnalysisRepository(session)
        self._mentions = EntityMentionRepository(session)
        self._attributions = ScanAnalysisRepository(session)

    def analyze(self, scan_id: uuid.UUID) -> ScanAnalysis:
        """Run or return the deterministic analysis for a scan.

        Idempotent: if a COMPLETED analysis exists for the current
        version, it is returned as-is. If a FAILED analysis exists,
        it is retried (old evidence deleted, new evidence inserted).
        """
        try:
            scan = self._scans.get_by_id(scan_id)
            if scan is None:
                raise NotFoundError("Scan not found.")

            self._validate_eligibility(scan)

            # Lock or create the analysis row.
            analysis = self._analyses.get_for_update(scan_id, ANALYSIS_VERSION)
            if analysis is None:
                analysis = ScanAnalysis(
                    scan_id=scan_id,
                    analysis_version=ANALYSIS_VERSION,
                    status=ScanAnalysisStatus.PENDING,
                )
                self._analyses.create(analysis)
                self._session.flush()
            elif analysis.status == ScanAnalysisStatus.COMPLETED:
                # Idempotent: return existing completed analysis.
                self._session.commit()
                return analysis
            elif analysis.status == ScanAnalysisStatus.RUNNING:
                # Another worker is analyzing. Return as-is.
                self._session.commit()
                return analysis

            # Mark RUNNING and clear any previous FAILED evidence.
            analysis.status = ScanAnalysisStatus.RUNNING
            analysis.started_at = datetime.now(UTC)
            analysis.failure_code = None
            analysis.failure_message = None
            self._session.flush()

            # If this is a retry of a FAILED analysis, delete old evidence.
            self._delete_evidence(analysis.id)

            result = self._run_analysis(scan, analysis)
            self._session.commit()
            return result
        except IntegrityError:
            self._session.rollback()
            # Concurrent insert won the race. Return the existing analysis.
            existing = self._analyses.get_by_scan_and_version(scan_id, ANALYSIS_VERSION)
            if existing is not None:
                return existing
            raise
        except Exception:
            self._session.rollback()
            # Mark the analysis as FAILED if it exists.
            self._mark_failed(scan_id)
            raise

    def _validate_eligibility(self, scan: Scan) -> None:
        if scan.status not in (ScanStatus.COMPLETED, ScanStatus.PARTIAL):
            raise ValidationError(
                "Analysis is not applicable to non-terminal scans or scans "
                "with no successful measurements."
            )
        if scan.successful_runs == 0:
            raise ValidationError(
                "Scan has no successful measurements; analysis is not applicable."
            )

    def _run_analysis(self, scan: Scan, analysis: ScanAnalysis) -> ScanAnalysis:
        # Load entity snapshots.
        snapshots = self._snapshots.list_by_scan(scan.id)
        if not snapshots:
            analysis.status = ScanAnalysisStatus.FAILED
            analysis.failure_code = "MISSING_ENTITY_SNAPSHOT"
            analysis.failure_message = (
                "Scan has no entity snapshots; cannot analyze with historical fidelity."
            )
            analysis.completed_at = datetime.now(UTC)
            self._session.flush()
            return analysis

        # Build snapshot dicts for the detection engines.
        snapshot_dicts = [
            {
                "entity_snapshot_id": str(snap.id),
                "name": snap.name,
                "domain": snap.domain,
                "aliases": snap.aliases,
            }
            for snap in snapshots
        ]

        # Build entity terms and detect ambiguous terms.
        entity_terms, term_warnings = build_entity_terms(snapshot_dicts)

        # Build entity domains for source attribution.
        entity_domains = build_entity_domains(snapshot_dicts)

        all_warnings: list[tuple[str, str]] = list(term_warnings)

        # Load SUCCEEDED runs with sources.
        succeeded_runs = self._load_succeeded_runs(scan.id)

        # Detect mentions and attribute sources for each run.
        all_mentions: list[EntityMention] = []
        all_attributions: list[SourceAttribution] = []

        # Build a lookup from snapshot ID → snapshot object.
        snapshot_map = {str(snap.id): snap for snap in snapshots}

        for run in succeeded_runs:
            # Mention detection
            if run.response_text:
                detection = detect_mentions(run.response_text, entity_terms)
                all_warnings.extend(detection.warnings)

                # Assign occurrence indices per (run, entity) pair.
                # occurrence_index follows response order, per entity per run.
                entity_occurrence_counter: dict[str, int] = {}
                for match in detection.mentions:
                    eid = match.entity_snapshot_id
                    entity_occurrence_counter[eid] = entity_occurrence_counter.get(eid, 0) + 1
                    snap = snapshot_map.get(eid)
                    if snap is None:
                        continue
                    all_mentions.append(
                        EntityMention(
                            scan_analysis_id=analysis.id,
                            prompt_run_id=run.id,
                            entity_snapshot_id=snap.id,
                            occurrence_index=entity_occurrence_counter[eid],
                            match_type=match.match_type,
                            matched_text=match.matched_text,
                            matched_term=match.matched_term,
                            start_index=match.start_index,
                            end_index=match.end_index,
                        )
                    )

            # Source attribution
            for source in run.sources:
                host = parse_source_host(source.url)
                if host is None:
                    all_warnings.append(
                        (
                            WarningCode.INVALID_SOURCE_URL.value,
                            f"Could not parse hostname from source URL: {source.url[:100]}",
                        )
                    )
                    continue
                attr = attribute_source(host, entity_domains)
                if attr is not None:
                    snap = snapshot_map.get(attr.entity_snapshot_id)
                    if snap is None:
                        continue
                    all_attributions.append(
                        SourceAttribution(
                            scan_analysis_id=analysis.id,
                            response_source_id=source.id,
                            entity_snapshot_id=snap.id,
                            source_host=attr.source_host,
                            attribution_type=AttributionType.OWNED_DOMAIN,
                        )
                    )

        # Persist evidence atomically.
        if all_mentions:
            self._mentions.create_batch(all_mentions)
        # Use the attributions repo — we need a SourceAttributionRepository.
        from app.repositories.analysis_repository import SourceAttributionRepository

        attr_repo = SourceAttributionRepository(self._session)
        if all_attributions:
            attr_repo.create_batch(all_attributions)

        # Cap warnings to a bounded number to prevent massive payloads.
        bounded_warnings = all_warnings[:100]
        analysis.warning_count = len(bounded_warnings)
        analysis.status = ScanAnalysisStatus.COMPLETED
        analysis.completed_at = datetime.now(UTC)
        self._session.flush()

        return analysis

    def _load_succeeded_runs(self, scan_id: uuid.UUID) -> list[PromptRun]:
        """Load SUCCEEDED runs with their sources eagerly loaded."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        runs = list(
            self._session.execute(
                select(PromptRun)
                .where(
                    PromptRun.scan_id == scan_id,
                    PromptRun.status == PromptRunStatus.SUCCEEDED,
                )
                .options(selectinload(PromptRun.sources))
                .order_by(PromptRun.created_at, PromptRun.id)
            ).scalars()
        )
        return runs

    def _delete_evidence(self, analysis_id: uuid.UUID) -> None:
        """Delete existing mentions and attributions for an analysis."""
        from app.repositories.analysis_repository import SourceAttributionRepository

        self._mentions.delete_by_analysis(analysis_id)
        SourceAttributionRepository(self._session).delete_by_analysis(analysis_id)

    def _mark_failed(self, scan_id: uuid.UUID) -> None:
        """Best-effort mark the analysis as FAILED after an exception."""
        try:
            analysis = self._analyses.get_for_update(scan_id, ANALYSIS_VERSION)
            if analysis is not None and analysis.status == ScanAnalysisStatus.RUNNING:
                analysis.status = ScanAnalysisStatus.FAILED
                analysis.failure_code = "INTERNAL_ERROR"
                analysis.failure_message = "Deterministic analysis failed unexpectedly."
                analysis.completed_at = datetime.now(UTC)
                self._session.commit()
        except Exception:
            self._session.rollback()
            logger.error("analysis_failed_mark_failed", scan_id=str(scan_id))

    def get_analysis(self, scan_id: uuid.UUID) -> ScanAnalysis | None:
        """Return the current-version analysis for a scan, or None."""
        return self._analyses.get_by_scan_and_version(scan_id, ANALYSIS_VERSION)
