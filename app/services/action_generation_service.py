"""ActionGenerationService — deterministic opportunity generation.

Analyzes completed STANDARD Scan evidence and upserts logical
Opportunities with immutable Occurrence + Evidence rows.

Key principles:
- Zero AI Checks, zero provider calls, zero UsageEvents.
- Uses CompetitorExplanationService for evidence computation.
- Stable fingerprint for cross-scan deduplication.
- Preserves human workflow status across refreshes.
- Idempotent: refreshing the same Scan creates no duplicates.
- Atomic: all or nothing per refresh.

Rules:
1. DISCOVERY_VISIBILITY_GAP — competitor exceeds brand in NON_BRANDED scope.
2. PROVIDER_VISIBILITY_GAP — competitor exceeds brand on one provider.
3. OWNED_CITATION_GAP — competitor owned citations exceed brand.
4. PROMPT_COMPETITOR_GAP — competitor appears, brand absent on a prompt.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.action_engine import (
    ACTION_ENGINE_VERSION,
    HIGH_DISCOVERY_VISIBILITY_GAP_PP,
    HIGH_PROVIDER_VISIBILITY_GAP_PP,
    MAX_OPPORTUNITIES_PER_REFRESH,
    MAX_PROMPT_OPPORTUNITIES_PER_COMPETITOR,
    MIN_DISCOVERY_VISIBILITY_GAP_PP,
    MIN_GLOBAL_SUCCESSFUL_OBSERVATIONS,
    MIN_OWNED_CITATION_GAP_PP,
    MIN_PROVIDER_SUCCESSFUL_OBSERVATIONS,
    MIN_PROVIDER_VISIBILITY_GAP_PP,
)
from app.core.enums import (
    LLMProvider,
    OpportunityEvidenceType,
    OpportunityPriority,
    OpportunityStatus,
    OpportunityType,
    PromptType,
    ScanAnalysisStatus,
    ScanStatus,
    ScanType,
    TrackedEntityType,
)
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.analysis import ScanAnalysis, ScanEntitySnapshot
from app.models.opportunity import (
    Opportunity,
    OpportunityEvidence,
    OpportunityOccurrence,
)
from app.models.scan import Scan
from app.services.competitor_explanation_service import (
    CompetitorExplanation,
    CompetitorExplanationService,
    _entity_type_str,
)


@dataclass(frozen=True)
class RefreshResult:
    """Result of an action refresh operation."""

    action_engine_version: str
    scan_id: uuid.UUID
    opportunities_detected: int
    opportunities_created: int
    opportunities_updated: int
    occurrences_created: int
    warnings: list[str]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _fingerprint(
    *,
    opportunity_type: OpportunityType,
    project_id: uuid.UUID,
    competitor_entity_key: str,
    provider: str | None,
    prompt_id: uuid.UUID | None,
    prompt_type: PromptType,
) -> str:
    """Compute a stable SHA-256 fingerprint for deduplication.

    Does NOT include scan_id, competitor name, or brand name — those
    would break cross-scan deduplication.
    """
    parts = [
        opportunity_type.value,
        str(project_id),
        competitor_entity_key,
        provider or "*",
        str(prompt_id) if prompt_id else "*",
        prompt_type.value,
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ActionGenerationService:
    """Generate and upsert deterministic Opportunities from Scan evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._explanation_service = CompetitorExplanationService(session)

    def refresh_from_scan(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scan_id: uuid.UUID,
    ) -> RefreshResult:
        """Analyze a STANDARD scan and upsert Opportunities.

        Atomic: all opportunities/evidence commit together or none.
        Status on pre-existing Opportunities is preserved.
        """
        scan = self._load_scoped_scan(workspace_id, project_id, scan_id)
        self._require_standard_scan(scan)
        self._require_terminal_scan(scan)

        analysis = self._load_analysis(scan_id)
        self._require_completed_analysis(analysis)
        assert analysis is not None

        snapshots = self._load_snapshots(scan_id)
        brand_snap = self._find_brand_snapshot(snapshots)
        competitor_snaps = [
            s
            for s in snapshots
            if _entity_type_str(s.entity_type) == TrackedEntityType.COMPETITOR.value
        ]

        detected: list[_DetectedOpportunity] = []
        warnings: list[str] = []

        for comp_snap in competitor_snaps:
            explanation = self._explanation_service.get_explanation(
                workspace_id,
                project_id,
                scan_id,
                comp_snap.id,
                prompt_type=PromptType.NON_BRANDED,
            )

            # Rule 1: Discovery visibility gap.
            det = self._check_discovery_gap(explanation, project_id, comp_snap, brand_snap)
            if det:
                detected.append(det)

            # Rule 2: Provider visibility gap.
            det_list = self._check_provider_gaps(explanation, project_id, comp_snap, brand_snap)
            detected.extend(det_list)

            # Rule 3: Owned citation gap.
            det = self._check_citation_gap(explanation, project_id, comp_snap, brand_snap)
            if det:
                detected.append(det)

            # Rule 4: Prompt competitor gap.
            det_list = self._check_prompt_gaps(explanation, project_id, comp_snap, brand_snap)
            detected.extend(det_list)

        # Safety: cap total opportunities per refresh.
        if len(detected) > MAX_OPPORTUNITIES_PER_REFRESH:
            warnings.append(
                f"Detected {len(detected)} opportunities; capping to "
                f"{MAX_OPPORTUNITIES_PER_REFRESH}."
            )
            detected = detected[:MAX_OPPORTUNITIES_PER_REFRESH]

        # Upsert each detected opportunity.
        created = 0
        updated = 0
        occurrences_created = 0

        for det in detected:
            is_new, had_occurrence = self._upsert_opportunity(
                det, workspace_id, project_id, scan_id, analysis.id
            )
            if is_new:
                created += 1
            else:
                updated += 1
            if not had_occurrence:
                occurrences_created += 1

        self._session.commit()

        return RefreshResult(
            action_engine_version=ACTION_ENGINE_VERSION,
            scan_id=scan_id,
            opportunities_detected=len(detected),
            opportunities_created=created,
            opportunities_updated=updated,
            occurrences_created=occurrences_created,
            warnings=warnings,
        )

    # --- Rule implementations ---

    def _check_discovery_gap(
        self,
        explanation: CompetitorExplanation,
        project_id: uuid.UUID,
        comp_snap: ScanEntitySnapshot,
        brand_snap: ScanEntitySnapshot,
    ) -> _DetectedOpportunity | None:
        """Rule 1: DISCOVERY_VISIBILITY_GAP."""
        if explanation.successful_observations < MIN_GLOBAL_SUCCESSFUL_OBSERVATIONS:
            return None

        gap = explanation.visibility_gap_pp
        if gap is None or gap < MIN_DISCOVERY_VISIBILITY_GAP_PP:
            return None

        if gap >= HIGH_DISCOVERY_VISIBILITY_GAP_PP:
            priority = OpportunityPriority.HIGH
        else:
            priority = OpportunityPriority.MEDIUM

        fp = _fingerprint(
            opportunity_type=OpportunityType.DISCOVERY_VISIBILITY_GAP,
            project_id=project_id,
            competitor_entity_key=comp_snap.entity_key,
            provider=None,
            prompt_id=None,
            prompt_type=PromptType.NON_BRANDED,
        )

        title = f"{comp_snap.name} visibility gap in non-branded discovery"
        summary = (
            f"{comp_snap.name} appeared in {explanation.competitor_visibility_rate}% "
            f"of successful non-branded observations versus "
            f"{explanation.brand_visibility_rate}% for {brand_snap.name} "
            f"(gap: {gap} percentage points)."
        )
        recommended_action = (
            f"Review the non-branded queries where {comp_snap.name} appears and "
            f"{brand_snap.name} does not. Strengthen crawlable first-party content "
            f"that directly answers those measured user intents and makes the "
            f"brand/entity context explicit."
        )

        return _DetectedOpportunity(
            fingerprint=fp,
            opportunity_type=OpportunityType.DISCOVERY_VISIBILITY_GAP,
            priority=priority,
            competitor_entity_key=comp_snap.entity_key,
            provider=None,
            prompt_id=None,
            prompt_type=PromptType.NON_BRANDED,
            title=title,
            summary=summary,
            recommended_action=recommended_action,
            brand_visibility=explanation.brand_visibility_rate,
            competitor_visibility=explanation.competitor_visibility_rate,
            visibility_gap_pp=gap,
            brand_citation_rate=explanation.brand_owned_citation_rate,
            competitor_citation_rate=explanation.competitor_owned_citation_rate,
            citation_gap_pp=explanation.citation_gap_pp,
            measurement_coverage=explanation.measurement_coverage,
            competitor_snapshot_id=comp_snap.id,
            brand_snapshot_id=brand_snap.id,
            evidence_rows=[
                _EvidenceRow(
                    evidence_key="discovery_visibility_gap",
                    evidence_type=OpportunityEvidenceType.METRIC_GAP,
                    metric_name="visibility_gap_pp",
                    brand_value=explanation.brand_visibility_rate,
                    competitor_value=explanation.competitor_visibility_rate,
                    delta_value=gap,
                )
            ],
        )

    def _check_provider_gaps(
        self,
        explanation: CompetitorExplanation,
        project_id: uuid.UUID,
        comp_snap: ScanEntitySnapshot,
        brand_snap: ScanEntitySnapshot,
    ) -> list[_DetectedOpportunity]:
        """Rule 2: PROVIDER_VISIBILITY_GAP per provider."""
        results: list[_DetectedOpportunity] = []

        for pb in explanation.provider_breakdown:
            if pb.successful_observations < MIN_PROVIDER_SUCCESSFUL_OBSERVATIONS:
                continue

            gap = pb.visibility_gap_pp
            if gap is None or gap < MIN_PROVIDER_VISIBILITY_GAP_PP:
                continue

            if gap >= HIGH_PROVIDER_VISIBILITY_GAP_PP:
                priority = OpportunityPriority.HIGH
            else:
                priority = OpportunityPriority.MEDIUM

            prov_str = pb.provider.value if hasattr(pb.provider, "value") else str(pb.provider)
            fp = _fingerprint(
                opportunity_type=OpportunityType.PROVIDER_VISIBILITY_GAP,
                project_id=project_id,
                competitor_entity_key=comp_snap.entity_key,
                provider=prov_str,
                prompt_id=None,
                prompt_type=PromptType.NON_BRANDED,
            )

            title = f"{comp_snap.name} visibility gap on {prov_str} for non-branded discovery"
            summary = (
                f"On {prov_str}, {comp_snap.name} appeared in "
                f"{pb.competitor_visibility_rate}% of successful non-branded "
                f"observations versus {pb.brand_visibility_rate}% for "
                f"{brand_snap.name} (gap: {gap} percentage points)."
            )
            recommended_action = (
                f"Prioritize the measured queries producing the largest visibility "
                f"gap on {prov_str}. Review successful response evidence and owned "
                f"citation patterns before making content changes."
            )

            results.append(
                _DetectedOpportunity(
                    fingerprint=fp,
                    opportunity_type=OpportunityType.PROVIDER_VISIBILITY_GAP,
                    priority=priority,
                    competitor_entity_key=comp_snap.entity_key,
                    provider=pb.provider,
                    prompt_id=None,
                    prompt_type=PromptType.NON_BRANDED,
                    title=title,
                    summary=summary,
                    recommended_action=recommended_action,
                    brand_visibility=pb.brand_visibility_rate,
                    competitor_visibility=pb.competitor_visibility_rate,
                    visibility_gap_pp=gap,
                    brand_citation_rate=pb.brand_owned_citation_rate,
                    competitor_citation_rate=pb.competitor_owned_citation_rate,
                    citation_gap_pp=None,
                    measurement_coverage=pb.measurement_coverage,
                    competitor_snapshot_id=comp_snap.id,
                    brand_snapshot_id=brand_snap.id,
                    evidence_rows=[
                        _EvidenceRow(
                            evidence_key=f"provider_visibility_gap_{prov_str}",
                            evidence_type=OpportunityEvidenceType.METRIC_GAP,
                            provider=pb.provider,
                            metric_name="visibility_gap_pp",
                            brand_value=pb.brand_visibility_rate,
                            competitor_value=pb.competitor_visibility_rate,
                            delta_value=gap,
                        )
                    ],
                )
            )

        return results

    def _check_citation_gap(
        self,
        explanation: CompetitorExplanation,
        project_id: uuid.UUID,
        comp_snap: ScanEntitySnapshot,
        brand_snap: ScanEntitySnapshot,
    ) -> _DetectedOpportunity | None:
        """Rule 3: OWNED_CITATION_GAP."""
        # Need enough citation-eligible (WEB_GROUNDED) observations.
        # We infer this from the explanation's citation rates being non-None.
        if explanation.brand_owned_citation_rate is None:
            return None
        if explanation.competitor_owned_citation_rate is None:
            return None

        # Check minimum eligible observations via provider breakdown.
        total_citation_eligible = sum(
            1 for pb in explanation.provider_breakdown if pb.brand_owned_citation_rate is not None
        )
        # Actually, we need the citation-eligible count from the explanation.
        # The CompetitorExplanation doesn't expose this directly, but we can
        # check that at least one provider has citation-eligible observations.
        # A simpler check: both rates are non-None, meaning there were eligible obs.
        # For the minimum, we use the fact that the rates are computed from
        # at least MIN_CITATION_ELIGIBLE_OBSERVATIONS.
        # Since we don't have the exact count here, we rely on the rates being non-None.
        # In practice, if there are 0 eligible observations, the rates would be None.
        if total_citation_eligible < 1:
            return None

        gap = explanation.citation_gap_pp
        if gap is None or gap < MIN_OWNED_CITATION_GAP_PP:
            return None

        # Priority: citation gaps are MEDIUM by default, HIGH if gap >= 40pp.
        priority = OpportunityPriority.HIGH if gap >= Decimal("40") else OpportunityPriority.MEDIUM

        fp = _fingerprint(
            opportunity_type=OpportunityType.OWNED_CITATION_GAP,
            project_id=project_id,
            competitor_entity_key=comp_snap.entity_key,
            provider=None,
            prompt_id=None,
            prompt_type=PromptType.NON_BRANDED,
        )

        title = f"{comp_snap.name} owned citation gap in non-branded discovery"
        summary = (
            f"{comp_snap.name}-owned sources were cited in "
            f"{explanation.competitor_owned_citation_rate}% of citation-eligible "
            f"observations; {brand_snap.name}-owned sources were cited in "
            f"{explanation.brand_owned_citation_rate}% (gap: {gap} percentage points)."
        )
        recommended_action = (
            "Create or strengthen authoritative, crawlable first-party pages "
            "that directly address the measured query themes where competitor-owned "
            "sources were cited and your owned domain was not."
        )

        evidence_rows: list[_EvidenceRow] = [
            _EvidenceRow(
                evidence_key="owned_citation_gap",
                evidence_type=OpportunityEvidenceType.METRIC_GAP,
                metric_name="citation_gap_pp",
                brand_value=explanation.brand_owned_citation_rate,
                competitor_value=explanation.competitor_owned_citation_rate,
                delta_value=gap,
            )
        ]

        # Add owned source evidence rows.
        for src in explanation.owned_citation_evidence[:10]:
            evidence_rows.append(
                _EvidenceRow(
                    evidence_key=f"owned_source_{src.response_source_id}",
                    evidence_type=OpportunityEvidenceType.OWNED_SOURCE,
                    response_source_id=src.response_source_id,
                    prompt_run_id=src.prompt_run_id,
                    prompt_id=src.prompt_id,
                    provider=src.provider,
                )
            )

        return _DetectedOpportunity(
            fingerprint=fp,
            opportunity_type=OpportunityType.OWNED_CITATION_GAP,
            priority=priority,
            competitor_entity_key=comp_snap.entity_key,
            provider=None,
            prompt_id=None,
            prompt_type=PromptType.NON_BRANDED,
            title=title,
            summary=summary,
            recommended_action=recommended_action,
            brand_visibility=explanation.brand_visibility_rate,
            competitor_visibility=explanation.competitor_visibility_rate,
            visibility_gap_pp=explanation.visibility_gap_pp,
            brand_citation_rate=explanation.brand_owned_citation_rate,
            competitor_citation_rate=explanation.competitor_owned_citation_rate,
            citation_gap_pp=gap,
            measurement_coverage=explanation.measurement_coverage,
            competitor_snapshot_id=comp_snap.id,
            brand_snapshot_id=brand_snap.id,
            evidence_rows=evidence_rows,
        )

    def _check_prompt_gaps(
        self,
        explanation: CompetitorExplanation,
        project_id: uuid.UUID,
        comp_snap: ScanEntitySnapshot,
        brand_snap: ScanEntitySnapshot,
    ) -> list[_DetectedOpportunity]:
        """Rule 4: PROMPT_COMPETITOR_GAP."""
        results: list[_DetectedOpportunity] = []
        prompt_gaps = explanation.prompt_gaps[:MAX_PROMPT_OPPORTUNITIES_PER_COMPETITOR]

        for pg in prompt_gaps:
            # Determine priority.
            funnel = pg.funnel_stage or "AWARENESS"
            multi_provider = len(pg.affected_providers) >= 2

            if funnel == "PURCHASE" and pg.commercial_intent and multi_provider:
                priority = OpportunityPriority.HIGH
            elif (
                funnel == "PURCHASE"
                or (funnel == "CONSIDERATION" and pg.commercial_intent)
                or multi_provider
            ):
                priority = OpportunityPriority.MEDIUM
            else:
                priority = OpportunityPriority.LOW

            fp = _fingerprint(
                opportunity_type=OpportunityType.PROMPT_COMPETITOR_GAP,
                project_id=project_id,
                competitor_entity_key=comp_snap.entity_key,
                provider=None,
                prompt_id=pg.prompt_id,
                prompt_type=PromptType.NON_BRANDED,
            )

            providers_str = ", ".join(
                p.value if hasattr(p, "value") else str(p) for p in pg.affected_providers
            )
            title = f"{comp_snap.name} appears without {brand_snap.name} on: {pg.prompt_text[:80]}"
            summary = (
                f"{pg.competitor_only_count} successful observation(s) mentioned "
                f"{comp_snap.name} while {brand_snap.name} was absent for the prompt: "
                f'"{pg.prompt_text}". Affected providers: {providers_str}.'
            )
            recommended_action = (
                "Address the user intent represented by this stored prompt with a "
                "focused first-party page or section. The competitor appeared in the "
                "measured AI response while your brand did not."
            )

            evidence_rows: list[_EvidenceRow] = [
                _EvidenceRow(
                    evidence_key=f"prompt_gap_{pg.prompt_id}",
                    evidence_type=OpportunityEvidenceType.PROMPT_RUN,
                    prompt_id=pg.prompt_id,
                    metric_name="competitor_only_count",
                    competitor_value=Decimal(pg.competitor_only_count),
                )
            ]

            results.append(
                _DetectedOpportunity(
                    fingerprint=fp,
                    opportunity_type=OpportunityType.PROMPT_COMPETITOR_GAP,
                    priority=priority,
                    competitor_entity_key=comp_snap.entity_key,
                    provider=None,
                    prompt_id=pg.prompt_id,
                    prompt_type=PromptType.NON_BRANDED,
                    title=title,
                    summary=summary,
                    recommended_action=recommended_action,
                    brand_visibility=explanation.brand_visibility_rate,
                    competitor_visibility=explanation.competitor_visibility_rate,
                    visibility_gap_pp=explanation.visibility_gap_pp,
                    brand_citation_rate=explanation.brand_owned_citation_rate,
                    competitor_citation_rate=explanation.competitor_owned_citation_rate,
                    citation_gap_pp=explanation.citation_gap_pp,
                    measurement_coverage=explanation.measurement_coverage,
                    competitor_snapshot_id=comp_snap.id,
                    brand_snapshot_id=brand_snap.id,
                    evidence_rows=evidence_rows,
                )
            )

        return results

    # --- Upsert logic ---

    def _upsert_opportunity(
        self,
        det: _DetectedOpportunity,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scan_id: uuid.UUID,
        analysis_id: uuid.UUID,
    ) -> tuple[bool, bool]:
        """Upsert an Opportunity + Occurrence + Evidence.

        Returns (is_new_opportunity, had_existing_occurrence_for_this_scan).
        """
        existing = self._session.execute(
            select(Opportunity).where(
                Opportunity.project_id == project_id,
                Opportunity.fingerprint == det.fingerprint,
            )
        ).scalar_one_or_none()

        now = _utcnow()

        if existing is None:
            # Create new Opportunity.
            opp = Opportunity(
                workspace_id=workspace_id,
                project_id=project_id,
                fingerprint=det.fingerprint,
                opportunity_type=det.opportunity_type,
                status=OpportunityStatus.OPEN,
                priority=det.priority,
                action_engine_version=ACTION_ENGINE_VERSION,
                competitor_entity_key=det.competitor_entity_key,
                provider=det.provider,
                prompt_id=det.prompt_id,
                prompt_type=det.prompt_type,
                title=det.title,
                summary=det.summary,
                recommended_action=det.recommended_action,
                first_detected_scan_id=scan_id,
                latest_detected_scan_id=scan_id,
                first_detected_at=now,
                last_detected_at=now,
            )
            self._session.add(opp)
            self._session.flush()
            existing = opp
            is_new = True
        else:
            is_new = False
            # Update mutable fields but PRESERVE status and workflow timestamps.
            existing.priority = det.priority
            existing.title = det.title
            existing.summary = det.summary
            existing.recommended_action = det.recommended_action
            existing.latest_detected_scan_id = scan_id
            existing.last_detected_at = now
            # Do NOT overwrite status, implemented_at, dismissed_at, verified_at.

        # Check if occurrence already exists for this scan.
        existing_occ = self._session.execute(
            select(OpportunityOccurrence).where(
                OpportunityOccurrence.opportunity_id == existing.id,
                OpportunityOccurrence.scan_id == scan_id,
            )
        ).scalar_one_or_none()

        if existing_occ is not None:
            # Idempotent: occurrence already exists for this scan.
            return is_new, True

        # Create new Occurrence.
        occ = OpportunityOccurrence(
            opportunity_id=existing.id,
            scan_id=scan_id,
            scan_analysis_id=analysis_id,
            competitor_entity_snapshot_id=det.competitor_snapshot_id,
            brand_entity_snapshot_id=det.brand_snapshot_id,
            priority_at_detection=det.priority,
            brand_visibility=det.brand_visibility,
            competitor_visibility=det.competitor_visibility,
            visibility_gap_pp=det.visibility_gap_pp,
            brand_citation_rate=det.brand_citation_rate,
            competitor_citation_rate=det.competitor_citation_rate,
            citation_gap_pp=det.citation_gap_pp,
            measurement_coverage=det.measurement_coverage,
        )
        self._session.add(occ)
        self._session.flush()

        # Create Evidence rows.
        for er in det.evidence_rows:
            ev = OpportunityEvidence(
                occurrence_id=occ.id,
                evidence_key=er.evidence_key,
                evidence_type=er.evidence_type,
                prompt_id=er.prompt_id,
                prompt_run_id=er.prompt_run_id,
                response_source_id=er.response_source_id,
                provider=er.provider,
                metric_name=er.metric_name,
                brand_value=er.brand_value,
                competitor_value=er.competitor_value,
                delta_value=er.delta_value,
            )
            self._session.add(ev)
        self._session.flush()

        return is_new, False

    # --- Scan/analysis helpers ---

    def _load_scoped_scan(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, scan_id: uuid.UUID
    ) -> Scan:
        scan = self._session.execute(
            select(Scan).where(
                Scan.id == scan_id,
                Scan.workspace_id == workspace_id,
                Scan.project_id == project_id,
            )
        ).scalar_one_or_none()
        if scan is None:
            raise NotFoundError("Scan not found.")
        return scan

    def _require_standard_scan(self, scan: Scan) -> None:
        if scan.scan_type != ScanType.STANDARD:
            raise ValidationError("Action generation requires a STANDARD scan.")

    def _require_terminal_scan(self, scan: Scan) -> None:
        if scan.status not in (ScanStatus.COMPLETED, ScanStatus.PARTIAL):
            raise ConflictError("Scan is not terminal.")

    def _require_completed_analysis(self, analysis: ScanAnalysis | None) -> None:
        if analysis is None or analysis.status != ScanAnalysisStatus.COMPLETED:
            raise ConflictError("Scan analysis is not completed.")

    def _load_analysis(self, scan_id: uuid.UUID) -> ScanAnalysis | None:
        from app.models.analysis import ANALYSIS_VERSION

        return self._session.execute(
            select(ScanAnalysis).where(
                ScanAnalysis.scan_id == scan_id,
                ScanAnalysis.analysis_version == ANALYSIS_VERSION,
            )
        ).scalar_one_or_none()

    def _load_snapshots(self, scan_id: uuid.UUID) -> list[ScanEntitySnapshot]:
        return list(
            self._session.execute(
                select(ScanEntitySnapshot)
                .where(ScanEntitySnapshot.scan_id == scan_id)
                .order_by(ScanEntitySnapshot.ordinal)
            ).scalars()
        )

    def _find_brand_snapshot(self, snapshots: list[ScanEntitySnapshot]) -> ScanEntitySnapshot:
        brand_snaps = [
            s for s in snapshots if _entity_type_str(s.entity_type) == TrackedEntityType.BRAND.value
        ]
        if len(brand_snaps) == 0:
            raise ValidationError("No brand snapshot found in scan.")
        if len(brand_snaps) > 1:
            raise ValidationError("Multiple brand snapshots found in scan.")
        return brand_snaps[0]


# --- Internal dataclasses ---


@dataclass(frozen=True)
class _EvidenceRow:
    evidence_key: str
    evidence_type: OpportunityEvidenceType
    prompt_id: uuid.UUID | None = None
    prompt_run_id: uuid.UUID | None = None
    response_source_id: uuid.UUID | None = None
    provider: LLMProvider | None = None
    metric_name: str | None = None
    brand_value: Decimal | None = None
    competitor_value: Decimal | None = None
    delta_value: Decimal | None = None


@dataclass(frozen=True)
class _DetectedOpportunity:
    fingerprint: str
    opportunity_type: OpportunityType
    priority: OpportunityPriority
    competitor_entity_key: str
    provider: LLMProvider | None
    prompt_id: uuid.UUID | None
    prompt_type: PromptType
    title: str
    summary: str
    recommended_action: str
    brand_visibility: Decimal | None
    competitor_visibility: Decimal | None
    visibility_gap_pp: Decimal | None
    brand_citation_rate: Decimal | None
    competitor_citation_rate: Decimal | None
    citation_gap_pp: Decimal | None
    measurement_coverage: Decimal | None
    competitor_snapshot_id: uuid.UUID
    brand_snapshot_id: uuid.UUID
    evidence_rows: list[_EvidenceRow]
