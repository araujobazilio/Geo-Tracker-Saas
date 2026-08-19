"""VisibilityMetricsService — deterministic metric computation from evidence.

Computes all Phase 7 metrics from persisted EntityMention and
SourceAttribution evidence. Does NOT persist opaque percentages —
metrics are recomputed from immutable evidence on every request.

Key formulas:
- Visibility Rate = (SUCCEEDED runs mentioning entity / all SUCCEEDED
  runs in scope) x 100
- Share of Voice = (entity mentioned-run count / sum of all entity
  mentioned-run counts) x 100, or NULL if denominator is 0
- Owned Citation Rate = (SUCCEEDED WEB_GROUNDED runs with >= 1 owned
  source / all SUCCEEDED WEB_GROUNDED runs in scope) x 100
- Measurement Coverage = (SUCCEEDED runs in scope / planned runs in
  scope) x 100

Zero vs NULL semantics:
- 0 successful observations → visibility_rate = NULL (no measurement)
- >0 successful observations, 0 mentions → visibility_rate = 0.0 (real zero)
- No entity mentioned anywhere → share_of_voice = NULL (no share to distribute)
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import (
    LLMProvider,
    PromptRunStatus,
    PromptType,
    ProviderExecutionMode,
    ScanAnalysisStatus,
    ScanStatus,
    TrackedEntityType,
)
from app.core.exceptions import NotFoundError
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


@dataclass(frozen=True)
class EntityMetric:
    entity_snapshot_id: uuid.UUID
    entity_type: str
    name: str
    domain: str
    planned_observations: int
    successful_observations: int
    mentioned_observations: int
    visibility_rate: Decimal | None
    share_of_voice: Decimal | None
    citation_eligible_observations: int
    owned_cited_observations: int
    owned_source_count: int
    owned_citation_rate: Decimal | None
    owned_source_share: Decimal | None


@dataclass(frozen=True)
class ProviderBreakdown:
    provider: LLMProvider
    successful_observations: int
    planned_observations: int
    measurement_coverage: Decimal | None
    visibility_rate: Decimal | None  # overall for this provider
    citation_eligible_observations: int
    owned_citation_rate: Decimal | None  # brand-only for provider breakdown


@dataclass(frozen=True)
class MetricsResult:
    scan_id: uuid.UUID
    scan_status: ScanStatus
    analysis_version: str | None
    analysis_status: ScanAnalysisStatus | None
    prompt_set_version: int
    scope: PromptType
    provider_filter: LLMProvider | None
    planned_observations: int
    successful_observations: int
    measurement_coverage: Decimal | None
    entity_metrics: list[EntityMetric]
    provider_breakdown: list[ProviderBreakdown]
    leaderboard: list[EntityMetric]
    warnings: list[tuple[str, str]] = field(default_factory=list)


class VisibilityMetricsService:
    """Compute deterministic visibility metrics from analysis evidence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_metrics(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scan_id: uuid.UUID,
        prompt_type: PromptType = PromptType.NON_BRANDED,
        provider: LLMProvider | None = None,
    ) -> MetricsResult:
        scan = self._load_scoped_scan(workspace_id, project_id, scan_id)
        analysis = self._load_analysis(scan_id)
        snapshots = self._load_snapshots(scan_id)

        # Load runs joined with prompts for prompt_type filtering.
        runs_with_prompts = self._load_runs_with_prompts(scan_id)
        succeeded_runs = [r for r in runs_with_prompts if r[0].status == PromptRunStatus.SUCCEEDED]

        # Filter by scope.
        scoped_succeeded = [
            r
            for r in succeeded_runs
            if r[1].prompt_type == prompt_type and (provider is None or r[0].provider == provider)
        ]
        scoped_planned = [
            r
            for r in runs_with_prompts
            if r[1].prompt_type == prompt_type and (provider is None or r[0].provider == provider)
        ]

        planned_count = len(scoped_planned)
        successful_count = len(scoped_succeeded)
        coverage = _pct(successful_count, planned_count)

        # Load mentions and attributions for the analysis.
        mentioned_run_ids_by_entity: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        owned_source_count_by_entity: dict[uuid.UUID, int] = defaultdict(int)
        owned_cited_run_ids_by_entity: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)

        if analysis is not None and analysis.status == ScanAnalysisStatus.COMPLETED:
            # Load mentions — only for runs in scope.
            scoped_succeeded_ids = {r[0].id for r in scoped_succeeded}
            mentions = self._load_mentions(analysis.id, scoped_succeeded_ids)
            for mention in mentions:
                mentioned_run_ids_by_entity[mention.entity_snapshot_id].add(mention.prompt_run_id)

            # Load attributions — need to join through response_sources → prompt_runs
            # to filter by scope.
            attributions = self._load_attributions(analysis.id)
            # Build source → run mapping.
            source_to_run = self._build_source_to_run_map(scan_id)
            for attr in attributions:
                run_id = source_to_run.get(attr.response_source_id)
                if run_id is None:
                    continue
                if run_id not in scoped_succeeded_ids:
                    continue
                # Check if the run is WEB_GROUNDED for citation eligibility.
                run = next((r for r in scoped_succeeded if r[0].id == run_id), None)
                if run is None or run[0].execution_mode != ProviderExecutionMode.WEB_GROUNDED:
                    continue
                owned_source_count_by_entity[attr.entity_snapshot_id] += 1
                owned_cited_run_ids_by_entity[attr.entity_snapshot_id].add(run_id)

        # Citation-eligible runs: SUCCEEDED WEB_GROUNDED in scope.
        citation_eligible_runs = [
            r for r in scoped_succeeded if r[0].execution_mode == ProviderExecutionMode.WEB_GROUNDED
        ]
        citation_eligible_count = len(citation_eligible_runs)

        # Total owned source count across all entities (for owned_source_share).
        total_owned_sources = sum(owned_source_count_by_entity.values())

        # Compute per-entity metrics.
        entity_metrics: list[EntityMetric] = []
        for snap in snapshots:
            mentioned_runs = mentioned_run_ids_by_entity.get(snap.id, set())
            mentioned_count = len(mentioned_runs)
            vis_rate = _pct(mentioned_count, successful_count)

            owned_cited = len(owned_cited_run_ids_by_entity.get(snap.id, set()))
            owned_sources = owned_source_count_by_entity.get(snap.id, 0)
            citation_rate = _pct(owned_cited, citation_eligible_count)
            source_share = (
                _pct(owned_sources, total_owned_sources) if total_owned_sources > 0 else None
            )

            entity_metrics.append(
                EntityMetric(
                    entity_snapshot_id=snap.id,
                    entity_type=(
                        snap.entity_type.value
                        if hasattr(snap.entity_type, "value")
                        else snap.entity_type
                    ),
                    name=snap.name,
                    domain=snap.domain,
                    planned_observations=planned_count,
                    successful_observations=successful_count,
                    mentioned_observations=mentioned_count,
                    visibility_rate=vis_rate,
                    share_of_voice=None,  # computed below
                    citation_eligible_observations=citation_eligible_count,
                    owned_cited_observations=owned_cited,
                    owned_source_count=owned_sources,
                    owned_citation_rate=citation_rate,
                    owned_source_share=source_share,
                )
            )

        # Compute Share of Voice.
        total_mentioned_run_presences = sum(em.mentioned_observations for em in entity_metrics)
        if total_mentioned_run_presences > 0:
            updated_metrics: list[EntityMetric] = []
            for em in entity_metrics:
                sov = _pct(em.mentioned_observations, total_mentioned_run_presences)
                updated_metrics.append(
                    EntityMetric(
                        entity_snapshot_id=em.entity_snapshot_id,
                        entity_type=em.entity_type,
                        name=em.name,
                        domain=em.domain,
                        planned_observations=em.planned_observations,
                        successful_observations=em.successful_observations,
                        mentioned_observations=em.mentioned_observations,
                        visibility_rate=em.visibility_rate,
                        share_of_voice=sov,
                        citation_eligible_observations=em.citation_eligible_observations,
                        owned_cited_observations=em.owned_cited_observations,
                        owned_source_count=em.owned_source_count,
                        owned_citation_rate=em.owned_citation_rate,
                        owned_source_share=em.owned_source_share,
                    )
                )
            entity_metrics = updated_metrics

        # Provider breakdown.
        provider_breakdown = self._compute_provider_breakdown(
            runs_with_prompts, prompt_type, snapshots, analysis
        )

        # Leaderboard: sort by visibility_rate desc, then name asc.
        # NULL visibility_rate sorts last.
        leaderboard = sorted(
            entity_metrics,
            key=lambda em: (
                em.visibility_rate is None,
                -(em.visibility_rate or Decimal(0)),
                em.name,
            ),
        )

        # PromptSet version
        from app.models.prompt_set import PromptSet

        prompt_set = self._session.get(PromptSet, scan.prompt_set_id)
        prompt_set_version = prompt_set.version if prompt_set else 0

        analysis_version = analysis.analysis_version if analysis else None
        analysis_status = analysis.status if analysis else None

        return MetricsResult(
            scan_id=scan_id,
            scan_status=scan.status,
            analysis_version=analysis_version,
            analysis_status=analysis_status,
            prompt_set_version=prompt_set_version,
            scope=prompt_type,
            provider_filter=provider,
            planned_observations=planned_count,
            successful_observations=successful_count,
            measurement_coverage=coverage,
            entity_metrics=entity_metrics,
            provider_breakdown=provider_breakdown,
            leaderboard=leaderboard,
        )

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

    def _load_runs_with_prompts(self, scan_id: uuid.UUID) -> list[tuple[PromptRun, Prompt]]:
        rows = self._session.execute(
            select(PromptRun, Prompt)
            .join(Prompt, PromptRun.prompt_id == Prompt.id)
            .where(PromptRun.scan_id == scan_id)
            .order_by(PromptRun.created_at, PromptRun.id)
        ).all()
        return [(run, prompt) for run, prompt in rows]  # noqa: C416

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
        return {source_id: run_id for source_id, run_id in rows}  # noqa: C416

    def _compute_provider_breakdown(
        self,
        runs_with_prompts: list[tuple[PromptRun, Prompt]],
        prompt_type: PromptType,
        snapshots: list[ScanEntitySnapshot],
        analysis: ScanAnalysis | None,
    ) -> list[ProviderBreakdown]:
        # Group runs by provider.
        providers: set[LLMProvider] = {r[0].provider for r in runs_with_prompts}

        # Load brand snapshot for provider-level visibility.
        brand_snap = next((s for s in snapshots if s.entity_type == TrackedEntityType.BRAND), None)

        breakdown: list[ProviderBreakdown] = []
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

            # Brand visibility for this provider.
            vis_rate: Decimal | None = None
            if brand_snap and analysis and analysis.status == ScanAnalysisStatus.COMPLETED:
                succeeded_ids = {r[0].id for r in succeeded}
                if succeeded_ids:
                    mentioned_count = int(
                        self._session.execute(
                            select(func.count(func.distinct(EntityMention.prompt_run_id))).where(
                                EntityMention.scan_analysis_id == analysis.id,
                                EntityMention.entity_snapshot_id == brand_snap.id,
                                EntityMention.prompt_run_id.in_(succeeded_ids),
                            )
                        ).scalar_one()
                    )
                    vis_rate = _pct(mentioned_count, successful)

            # Citation rate for this provider (brand only).
            citation_eligible = [
                r for r in succeeded if r[0].execution_mode == ProviderExecutionMode.WEB_GROUNDED
            ]
            citation_count = len(citation_eligible)
            owned_cited = 0
            citation_rate: Decimal | None = None
            if brand_snap and analysis and analysis.status == ScanAnalysisStatus.COMPLETED:
                cited_run_ids: set[uuid.UUID] = set()
                if citation_count > 0:
                    eligible_ids = {r[0].id for r in citation_eligible}
                    source_to_run = self._build_source_to_run_map(
                        runs_with_prompts[0][0].scan_id if runs_with_prompts else uuid.UUID(int=0)
                    )
                    attributions = self._load_attributions(analysis.id)
                    for attr in attributions:
                        if attr.entity_snapshot_id != brand_snap.id:
                            continue
                        run_id = source_to_run.get(attr.response_source_id)
                        if run_id and run_id in eligible_ids:
                            cited_run_ids.add(run_id)
                    owned_cited = len(cited_run_ids)
                citation_rate = _pct(owned_cited, citation_count)

            breakdown.append(
                ProviderBreakdown(
                    provider=prov,
                    successful_observations=successful,
                    planned_observations=planned,
                    measurement_coverage=coverage,
                    visibility_rate=vis_rate,
                    citation_eligible_observations=citation_count,
                    owned_citation_rate=citation_rate,
                )
            )

        return breakdown
