"""CompetitorExplanationService — evidence-based competitor explanation.

Computes deterministic, evidence-based explanations comparing a brand
vs a single competitor from persisted Scan analysis evidence.

Key principles:
- Zero AI Checks, zero provider calls, zero UsageEvents.
- Uses ONLY persisted evidence (PromptRun, EntityMention,
  SourceAttribution, ScanEntitySnapshot).
- Requires COMPLETED analysis (fail closed).
- Uses historical ScanEntitySnapshot, not current mutable Project state.
- No causal claims — only observed gaps and measured differences.
- Optional Confidence context from a linked CONFIDENCE Scan.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    LLMProvider,
    PromptRunStatus,
    PromptType,
    ProviderExecutionMode,
    ScanAnalysisStatus,
    ScanStatus,
    ScanType,
    TrackedEntityType,
)
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.analysis import (
    ANALYSIS_VERSION,
    EntityMention,
    ScanAnalysis,
    ScanEntitySnapshot,
    SourceAttribution,
)
from app.models.scan import PromptRun, ResponseSource, Scan
from app.models.tracking import Prompt

_PRECISION = Decimal("0.0001")


def _pct(numerator: int, denominator: int) -> Decimal | None:
    """Compute a percentage as Decimal, or None if denominator is 0."""
    if denominator == 0:
        return None
    return (Decimal(numerator) / Decimal(denominator) * Decimal(100)).quantize(
        _PRECISION, rounding=ROUND_HALF_UP
    )


def _entity_type_str(et: object) -> str:
    """Safely extract string value from TrackedEntityType."""
    val = getattr(et, "value", None)
    if val is not None:
        return str(val)
    return str(et)


@dataclass(frozen=True)
class OverlapMatrix:
    """Classification of SUCCEEDED runs by brand/competitor mention."""

    brand_only_runs: int
    competitor_only_runs: int
    both_runs: int
    neither_runs: int
    successful_observations: int

    @property
    def competitor_only_rate(self) -> Decimal | None:
        return _pct(self.competitor_only_runs, self.successful_observations)


@dataclass(frozen=True)
class ProviderExplanation:
    """Per-provider brand vs competitor comparison."""

    provider: LLMProvider
    planned_observations: int
    successful_observations: int
    measurement_coverage: Decimal | None
    brand_visibility_rate: Decimal | None
    competitor_visibility_rate: Decimal | None
    visibility_gap_pp: Decimal | None
    brand_owned_citation_rate: Decimal | None
    competitor_owned_citation_rate: Decimal | None
    competitor_only_runs: int


@dataclass(frozen=True)
class PromptGapEvidence:
    """A stored prompt where competitor appears and brand does not."""

    prompt_id: uuid.UUID
    prompt_text: str
    prompt_type: PromptType
    intent: str | None
    funnel_stage: str | None
    commercial_intent: bool
    affected_providers: list[LLMProvider]
    successful_observations: int
    competitor_only_count: int


@dataclass(frozen=True)
class OwnedCitationEvidence:
    """A competitor-owned source citation from provider response."""

    response_source_id: uuid.UUID
    url: str
    title: str | None
    provider: LLMProvider
    prompt_run_id: uuid.UUID
    prompt_id: uuid.UUID


@dataclass(frozen=True)
class ReliabilityContext:
    """Optional Confidence Scan reliability context for an entity."""

    confidence_scan_id: uuid.UUID
    overall_visibility_rate: Decimal | None
    mention_stability: Decimal | None
    repeat_sufficiency: Decimal | None
    observed_visibility_min: Decimal | None
    observed_visibility_max: Decimal | None
    confidence_level: str
    confidence_methodology_version: str


@dataclass(frozen=True)
class CompetitorExplanation:
    """Full evidence-based explanation for brand vs competitor."""

    scan_id: uuid.UUID
    competitor_entity_snapshot_id: uuid.UUID
    competitor_entity_key: str
    competitor_name: str
    competitor_domain: str
    brand_entity_snapshot_id: uuid.UUID
    brand_name: str
    brand_domain: str

    prompt_type: PromptType
    provider_filter: LLMProvider | None

    brand_visibility_rate: Decimal | None
    competitor_visibility_rate: Decimal | None
    visibility_gap_pp: Decimal | None

    brand_share_of_voice: Decimal | None
    competitor_share_of_voice: Decimal | None

    brand_owned_citation_rate: Decimal | None
    competitor_owned_citation_rate: Decimal | None
    citation_gap_pp: Decimal | None

    successful_observations: int
    measurement_coverage: Decimal | None

    overlap: OverlapMatrix

    provider_breakdown: list[ProviderExplanation]
    prompt_gaps: list[PromptGapEvidence]
    owned_citation_evidence: list[OwnedCitationEvidence]

    reliability_context: ReliabilityContext | None = None


class CompetitorExplanationService:
    """Compute evidence-based competitor explanations from persisted evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_explanation(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scan_id: uuid.UUID,
        competitor_snapshot_id: uuid.UUID,
        prompt_type: PromptType = PromptType.NON_BRANDED,
        provider: LLMProvider | None = None,
    ) -> CompetitorExplanation:
        scan = self._load_scoped_scan(workspace_id, project_id, scan_id)
        self._require_standard_scan(scan)
        self._require_terminal_scan(scan)

        analysis = self._load_analysis(scan_id)
        self._require_completed_analysis(analysis)
        assert analysis is not None  # type narrowing

        snapshots = self._load_snapshots(scan_id)
        brand_snap = self._find_brand_snapshot(snapshots)
        competitor_snap = self._find_competitor_snapshot(snapshots, competitor_snapshot_id)

        # Validate competitor snapshot belongs to this scan.
        if competitor_snap.scan_id != scan_id:
            raise NotFoundError("Competitor snapshot not found in this scan.")
        if _entity_type_str(competitor_snap.entity_type) != TrackedEntityType.COMPETITOR.value:
            raise ValidationError("Requested snapshot is not a competitor.")

        # Load runs joined with prompts.
        runs_with_prompts = self._load_runs_with_prompts(scan_id)
        succeeded = [r for r in runs_with_prompts if r[0].status == PromptRunStatus.SUCCEEDED]
        scoped_succeeded = [
            r
            for r in succeeded
            if r[1].prompt_type == prompt_type and (provider is None or r[0].provider == provider)
        ]
        scoped_planned = [
            r
            for r in runs_with_prompts
            if r[1].prompt_type == prompt_type and (provider is None or r[0].provider == provider)
        ]

        successful_count = len(scoped_succeeded)
        planned_count = len(scoped_planned)
        coverage = _pct(successful_count, planned_count)

        # Load mentions for scoped succeeded runs.
        scoped_succeeded_ids = {r[0].id for r in scoped_succeeded}
        mentions = self._load_mentions(analysis.id, scoped_succeeded_ids)

        brand_mentioned_runs: set[uuid.UUID] = set()
        competitor_mentioned_runs: set[uuid.UUID] = set()
        for m in mentions:
            if m.entity_snapshot_id == brand_snap.id:
                brand_mentioned_runs.add(m.prompt_run_id)
            elif m.entity_snapshot_id == competitor_snap.id:
                competitor_mentioned_runs.add(m.prompt_run_id)

        brand_vis = _pct(len(brand_mentioned_runs), successful_count)
        competitor_vis = _pct(len(competitor_mentioned_runs), successful_count)
        gap_pp = self._gap_pp(competitor_vis, brand_vis)

        # Share of voice: entity mentioned / total mentioned presences.
        total_mentioned_presences = len(brand_mentioned_runs) + len(competitor_mentioned_runs)
        brand_sov = _pct(len(brand_mentioned_runs), total_mentioned_presences)
        competitor_sov = _pct(len(competitor_mentioned_runs), total_mentioned_presences)

        # Overlap matrix.
        brand_only = len(brand_mentioned_runs - competitor_mentioned_runs)
        competitor_only = len(competitor_mentioned_runs - brand_mentioned_runs)
        both = len(brand_mentioned_runs & competitor_mentioned_runs)
        neither = successful_count - brand_only - competitor_only - both

        overlap = OverlapMatrix(
            brand_only_runs=brand_only,
            competitor_only_runs=competitor_only,
            both_runs=both,
            neither_runs=neither,
            successful_observations=successful_count,
        )

        # Citation metrics.
        citation_eligible = [
            r for r in scoped_succeeded if r[0].execution_mode == ProviderExecutionMode.WEB_GROUNDED
        ]
        citation_eligible_count = len(citation_eligible)
        citation_eligible_ids = {r[0].id for r in citation_eligible}

        attributions = self._load_attributions(analysis.id)
        source_to_run = self._build_source_to_run_map(scan_id)

        brand_cited_runs: set[uuid.UUID] = set()
        competitor_cited_runs: set[uuid.UUID] = set()
        competitor_citation_evidence: list[OwnedCitationEvidence] = []

        # Build run -> prompt mapping and source lookup.
        run_to_prompt: dict[uuid.UUID, uuid.UUID] = {r[0].id: r[1].id for r in scoped_succeeded}
        run_to_provider: dict[uuid.UUID, LLMProvider] = {
            r[0].id: r[0].provider for r in scoped_succeeded
        }
        source_map: dict[uuid.UUID, ResponseSource] = {
            s.id: s
            for s in self._session.execute(
                select(ResponseSource).where(
                    ResponseSource.prompt_run_id.in_(
                        {r[0].id for r in scoped_succeeded} or {uuid.UUID(int=0)}
                    )
                )
            ).scalars()
        }

        for attr in attributions:
            run_id = source_to_run.get(attr.response_source_id)
            if run_id is None or run_id not in citation_eligible_ids:
                continue
            if attr.entity_snapshot_id == brand_snap.id:
                brand_cited_runs.add(run_id)
            elif attr.entity_snapshot_id == competitor_snap.id:
                competitor_cited_runs.add(run_id)
                src = source_map.get(attr.response_source_id)
                if src:
                    competitor_citation_evidence.append(
                        OwnedCitationEvidence(
                            response_source_id=src.id,
                            url=src.url,
                            title=src.title,
                            provider=run_to_provider.get(run_id, LLMProvider.OPENAI),
                            prompt_run_id=run_id,
                            prompt_id=run_to_prompt.get(run_id, uuid.UUID(int=0)),
                        )
                    )

        brand_citation_rate = _pct(len(brand_cited_runs), citation_eligible_count)
        competitor_citation_rate = _pct(len(competitor_cited_runs), citation_eligible_count)
        citation_gap_pp = self._gap_pp(competitor_citation_rate, brand_citation_rate)

        # Provider breakdown.
        provider_breakdown = self._compute_provider_breakdown(
            runs_with_prompts, prompt_type, brand_snap, competitor_snap, analysis
        )

        # Prompt gaps: prompts where competitor mentioned, brand not.
        prompt_gaps = self._compute_prompt_gaps(
            scoped_succeeded, brand_mentioned_runs, competitor_mentioned_runs
        )

        # Reliability context (optional).
        reliability = self._load_reliability_context(
            workspace_id, project_id, scan_id, brand_snap, competitor_snap
        )

        return CompetitorExplanation(
            scan_id=scan_id,
            competitor_entity_snapshot_id=competitor_snap.id,
            competitor_entity_key=competitor_snap.entity_key,
            competitor_name=competitor_snap.name,
            competitor_domain=competitor_snap.domain,
            brand_entity_snapshot_id=brand_snap.id,
            brand_name=brand_snap.name,
            brand_domain=brand_snap.domain,
            prompt_type=prompt_type,
            provider_filter=provider,
            brand_visibility_rate=brand_vis,
            competitor_visibility_rate=competitor_vis,
            visibility_gap_pp=gap_pp,
            brand_share_of_voice=brand_sov,
            competitor_share_of_voice=competitor_sov,
            brand_owned_citation_rate=brand_citation_rate,
            competitor_owned_citation_rate=competitor_citation_rate,
            citation_gap_pp=citation_gap_pp,
            successful_observations=successful_count,
            measurement_coverage=coverage,
            overlap=overlap,
            provider_breakdown=provider_breakdown,
            prompt_gaps=prompt_gaps,
            owned_citation_evidence=competitor_citation_evidence,
            reliability_context=reliability,
        )

    def list_competitor_summaries(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scan_id: uuid.UUID,
        prompt_type: PromptType = PromptType.NON_BRANDED,
    ) -> list[CompetitorSummary]:
        """Return one evidence-based summary per COMPETITOR snapshot."""
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

        summaries: list[CompetitorSummary] = []
        for comp_snap in competitor_snaps:
            explanation = self.get_explanation(
                workspace_id, project_id, scan_id, comp_snap.id, prompt_type
            )
            reliability = self._load_reliability_context(
                workspace_id, project_id, scan_id, brand_snap, comp_snap
            )
            summaries.append(
                CompetitorSummary(
                    entity_snapshot_id=comp_snap.id,
                    entity_key=comp_snap.entity_key,
                    name=comp_snap.name,
                    domain=comp_snap.domain,
                    brand_visibility_rate=explanation.brand_visibility_rate,
                    competitor_visibility_rate=explanation.competitor_visibility_rate,
                    visibility_gap_pp=explanation.visibility_gap_pp,
                    brand_owned_citation_rate=explanation.brand_owned_citation_rate,
                    competitor_owned_citation_rate=explanation.competitor_owned_citation_rate,
                    citation_gap_pp=explanation.citation_gap_pp,
                    competitor_only_runs=explanation.overlap.competitor_only_runs,
                    reliability_context=reliability,
                )
            )
        return summaries

    # --- Helpers ---

    def _gap_pp(self, competitor: Decimal | None, brand: Decimal | None) -> Decimal | None:
        if competitor is None or brand is None:
            return None
        return (competitor - brand).quantize(_PRECISION, rounding=ROUND_HALF_UP)

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
            raise ValidationError("Competitor explanation requires a STANDARD scan.")

    def _require_terminal_scan(self, scan: Scan) -> None:
        if scan.status not in (ScanStatus.COMPLETED, ScanStatus.PARTIAL):
            raise ConflictError("Scan is not terminal.")

    def _require_completed_analysis(self, analysis: ScanAnalysis | None) -> None:
        if analysis is None or analysis.status != ScanAnalysisStatus.COMPLETED:
            raise ConflictError("Scan analysis is not completed.")

    def _load_analysis(self, scan_id: uuid.UUID) -> ScanAnalysis | None:
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

    def _find_competitor_snapshot(
        self, snapshots: list[ScanEntitySnapshot], snapshot_id: uuid.UUID
    ) -> ScanEntitySnapshot:
        snap = next((s for s in snapshots if s.id == snapshot_id), None)
        if snap is None:
            raise NotFoundError("Competitor snapshot not found.")
        return snap

    def _load_runs_with_prompts(self, scan_id: uuid.UUID) -> list[tuple[PromptRun, Prompt]]:
        rows = self._session.execute(
            select(PromptRun, Prompt)
            .join(Prompt, PromptRun.prompt_id == Prompt.id)
            .where(PromptRun.scan_id == scan_id)
            .order_by(PromptRun.created_at, PromptRun.id)
        ).all()
        result: list[tuple[PromptRun, Prompt]] = []
        for run, prompt in rows:
            result.append((run, prompt))
        return result

    def _load_mentions(
        self, analysis_id: uuid.UUID, run_ids: set[uuid.UUID]
    ) -> list[EntityMention]:
        if not run_ids:
            return []
        return list(
            self._session.execute(
                select(EntityMention).where(
                    EntityMention.scan_analysis_id == analysis_id,
                    EntityMention.prompt_run_id.in_(run_ids),
                )
            ).scalars()
        )

    def _load_attributions(self, analysis_id: uuid.UUID) -> list[SourceAttribution]:
        return list(
            self._session.execute(
                select(SourceAttribution).where(
                    SourceAttribution.scan_analysis_id == analysis_id,
                )
            ).scalars()
        )

    def _build_source_to_run_map(self, scan_id: uuid.UUID) -> dict[uuid.UUID, uuid.UUID]:
        rows = self._session.execute(
            select(ResponseSource.id, PromptRun.id)
            .join(PromptRun, ResponseSource.prompt_run_id == PromptRun.id)
            .where(PromptRun.scan_id == scan_id)
        ).all()
        result: dict[uuid.UUID, uuid.UUID] = {}
        for source_id, run_id in rows:
            result[source_id] = run_id
        return result

    def _compute_provider_breakdown(
        self,
        runs_with_prompts: list[tuple[PromptRun, Prompt]],
        prompt_type: PromptType,
        brand_snap: ScanEntitySnapshot,
        competitor_snap: ScanEntitySnapshot,
        analysis: ScanAnalysis,
    ) -> list[ProviderExplanation]:
        providers: set[LLMProvider] = {r[0].provider for r in runs_with_prompts}
        breakdown: list[ProviderExplanation] = []

        for prov in sorted(providers, key=lambda p: p.value if hasattr(p, "value") else p):
            scoped = [
                r
                for r in runs_with_prompts
                if r[0].provider == prov and r[1].prompt_type == prompt_type
            ]
            succeeded = [r for r in scoped if r[0].status == PromptRunStatus.SUCCEEDED]
            planned = len(scoped)
            successful = len(succeeded)
            coverage = _pct(successful, planned)

            succeeded_ids = {r[0].id for r in succeeded}

            # Brand and competitor visibility for this provider.
            brand_mentioned = 0
            competitor_mentioned = 0
            competitor_only = 0
            if succeeded_ids:
                mentions = list(
                    self._session.execute(
                        select(EntityMention).where(
                            EntityMention.scan_analysis_id == analysis.id,
                            EntityMention.prompt_run_id.in_(succeeded_ids),
                            EntityMention.entity_snapshot_id.in_(
                                [brand_snap.id, competitor_snap.id]
                            ),
                        )
                    ).scalars()
                )
                brand_runs: set[uuid.UUID] = set()
                competitor_runs: set[uuid.UUID] = set()
                for m in mentions:
                    if m.entity_snapshot_id == brand_snap.id:
                        brand_runs.add(m.prompt_run_id)
                    elif m.entity_snapshot_id == competitor_snap.id:
                        competitor_runs.add(m.prompt_run_id)
                brand_mentioned = len(brand_runs)
                competitor_mentioned = len(competitor_runs)
                competitor_only = len(competitor_runs - brand_runs)

            brand_vis = _pct(brand_mentioned, successful)
            competitor_vis = _pct(competitor_mentioned, successful)
            gap_pp = self._gap_pp(competitor_vis, brand_vis)

            # Citation rates per provider.
            citation_eligible = [
                r for r in succeeded if r[0].execution_mode == ProviderExecutionMode.WEB_GROUNDED
            ]
            citation_count = len(citation_eligible)
            eligible_ids = {r[0].id for r in citation_eligible}

            brand_cited = 0
            competitor_cited = 0
            if eligible_ids:
                source_to_run = self._build_source_to_run_map(
                    runs_with_prompts[0][0].scan_id if runs_with_prompts else uuid.UUID(int=0)
                )
                attributions = self._load_attributions(analysis.id)
                brand_cited_runs: set[uuid.UUID] = set()
                competitor_cited_runs: set[uuid.UUID] = set()
                for attr in attributions:
                    run_id = source_to_run.get(attr.response_source_id)
                    if run_id is None or run_id not in eligible_ids:
                        continue
                    if attr.entity_snapshot_id == brand_snap.id:
                        brand_cited_runs.add(run_id)
                    elif attr.entity_snapshot_id == competitor_snap.id:
                        competitor_cited_runs.add(run_id)
                brand_cited = len(brand_cited_runs)
                competitor_cited = len(competitor_cited_runs)

            brand_citation_rate = _pct(brand_cited, citation_count)
            competitor_citation_rate = _pct(competitor_cited, citation_count)

            breakdown.append(
                ProviderExplanation(
                    provider=prov,
                    planned_observations=planned,
                    successful_observations=successful,
                    measurement_coverage=coverage,
                    brand_visibility_rate=brand_vis,
                    competitor_visibility_rate=competitor_vis,
                    visibility_gap_pp=gap_pp,
                    brand_owned_citation_rate=brand_citation_rate,
                    competitor_owned_citation_rate=competitor_citation_rate,
                    competitor_only_runs=competitor_only,
                )
            )

        return breakdown

    def _compute_prompt_gaps(
        self,
        scoped_succeeded: list[tuple[PromptRun, Prompt]],
        brand_mentioned_runs: set[uuid.UUID],
        competitor_mentioned_runs: set[uuid.UUID],
    ) -> list[PromptGapEvidence]:
        """Find prompts where competitor appears and brand does not."""
        # Group by prompt_id using a typed structure.
        prompt_providers: dict[uuid.UUID, set[LLMProvider]] = defaultdict(set)
        prompt_successful: dict[uuid.UUID, int] = defaultdict(int)
        prompt_competitor_only: dict[uuid.UUID, int] = defaultdict(int)
        prompt_obj: dict[uuid.UUID, Prompt] = {}

        for run, prompt in scoped_succeeded:
            pid = prompt.id
            prompt_obj[pid] = prompt
            prompt_providers[pid].add(run.provider)
            prompt_successful[pid] += 1

            is_brand = run.id in brand_mentioned_runs
            is_comp = run.id in competitor_mentioned_runs
            if is_comp and not is_brand:
                prompt_competitor_only[pid] += 1

        # Filter to prompts with at least 1 competitor-only observation.
        gaps: list[PromptGapEvidence] = []
        for pid, comp_only_count in prompt_competitor_only.items():
            if comp_only_count == 0:
                continue
            prompt = prompt_obj[pid]
            funnel = prompt.funnel_stage
            gaps.append(
                PromptGapEvidence(
                    prompt_id=pid,
                    prompt_text=prompt.text,
                    prompt_type=prompt.prompt_type,
                    intent=prompt.intent,
                    funnel_stage=(funnel.value if hasattr(funnel, "value") else funnel)
                    if funnel
                    else None,
                    commercial_intent=prompt.commercial_intent,
                    affected_providers=sorted(
                        prompt_providers[pid],
                        key=lambda p: p.value if hasattr(p, "value") else p,
                    ),
                    successful_observations=prompt_successful[pid],
                    competitor_only_count=comp_only_count,
                )
            )

        # Sort by priority: PURCHASE+commercial > PURCHASE > CONSIDERATION+commercial
        # > CONSIDERATION > AWARENESS, then providers desc, then variant_index.
        funnel_order = {"PURCHASE": 0, "CONSIDERATION": 1, "AWARENESS": 2}
        gaps.sort(
            key=lambda g: (
                funnel_order.get(g.funnel_stage or "AWARENESS", 3),
                not g.commercial_intent,
                -len(g.affected_providers),
                g.prompt_id,
            )
        )
        return gaps

    def _load_reliability_context(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        standard_scan_id: uuid.UUID,
        brand_snap: ScanEntitySnapshot,
        competitor_snap: ScanEntitySnapshot,
    ) -> ReliabilityContext | None:
        """Find the most recent completed CONFIDENCE scan linked to this STANDARD."""
        # Find CONFIDENCE scans with baseline_scan_id == standard_scan_id.
        conf_scans = list(
            self._session.execute(
                select(Scan)
                .where(
                    Scan.baseline_scan_id == standard_scan_id,
                    Scan.scan_type == ScanType.CONFIDENCE,
                    Scan.workspace_id == workspace_id,
                    Scan.project_id == project_id,
                    Scan.status.in_([ScanStatus.COMPLETED, ScanStatus.PARTIAL]),
                )
                .order_by(Scan.completed_at.desc())
            ).scalars()
        )

        for conf_scan in conf_scans:
            conf_analysis = self._session.execute(
                select(ScanAnalysis).where(
                    ScanAnalysis.scan_id == conf_scan.id,
                    ScanAnalysis.status == ScanAnalysisStatus.COMPLETED,
                )
            ).scalar_one_or_none()
            if conf_analysis is None:
                continue

            # Use ConfidenceMetricsService to get reliability for this scan.
            from app.services.confidence_metrics_service import ConfidenceMetricsService

            try:
                result = ConfidenceMetricsService(self._session).get_metrics(
                    workspace_id,
                    project_id,
                    conf_scan.id,
                    prompt_type=PromptType.NON_BRANDED,
                )
            except (ConflictError, NotFoundError, ValidationError):
                continue

            # Match entity by entity_key (not snapshot UUID).
            brand_reliability = next(
                (
                    er
                    for er in result.entity_reliability
                    if self._match_entity_key(er, brand_snap, conf_scan.id)
                ),
                None,
            )
            if brand_reliability is None:
                continue

            return ReliabilityContext(
                confidence_scan_id=conf_scan.id,
                overall_visibility_rate=brand_reliability.overall_visibility_rate,
                mention_stability=brand_reliability.mention_stability,
                repeat_sufficiency=brand_reliability.repeat_sufficiency,
                observed_visibility_min=brand_reliability.observed_visibility_min,
                observed_visibility_max=brand_reliability.observed_visibility_max,
                confidence_level=brand_reliability.confidence_level,
                confidence_methodology_version=result.confidence_methodology_version,
            )

        return None

    def _match_entity_key(
        self, er: object, snap: ScanEntitySnapshot, conf_scan_id: uuid.UUID
    ) -> bool:
        """Match entity reliability to snapshot by entity_key via confidence scan snapshots."""
        conf_snaps = self._load_snapshots(conf_scan_id)
        for cs in conf_snaps:
            if cs.entity_key == snap.entity_key and cs.id == getattr(
                er, "entity_snapshot_id", None
            ):
                return True
        return False


@dataclass(frozen=True)
class CompetitorSummary:
    """Per-competitor summary for the list endpoint."""

    entity_snapshot_id: uuid.UUID
    entity_key: str
    name: str
    domain: str
    brand_visibility_rate: Decimal | None
    competitor_visibility_rate: Decimal | None
    visibility_gap_pp: Decimal | None
    brand_owned_citation_rate: Decimal | None
    competitor_owned_citation_rate: Decimal | None
    citation_gap_pp: Decimal | None
    competitor_only_runs: int
    reliability_context: ReliabilityContext | None = None
