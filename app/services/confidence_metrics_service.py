"""ConfidenceMetricsService -- reliability metrics for CONFIDENCE scans.

Computes measurement reliability from repeated observations of the same
Prompt x Provider cells. This is NOT a statistical confidence interval.
It is a deterministic, transparent evidence-quality heuristic.

Key concepts:
- Measurement Cell = Prompt ID x Provider (within the Confidence Scan)
- Each cell plans repeat_count observations
- Repeat-analyzable cell: >= 2 successful repeats
- Stable cell: all successful repeats agree (all mention OR all no-mention)
- Variable cell: some mention, some don't (among successful repeats)
- Mention Stability = stable repeat-analyzable cells / all repeat-analyzable cells
- Repeat Sufficiency = cells with >= 2 successful repeats / all planned cells
- Round Visibility = ordinary Phase 7 visibility per observation_index
- Observed Visibility Range = max(round visibilities) - min(round visibilities)

Analysis readiness:
- Metrics require a COMPLETED ScanAnalysis. A missing, PENDING, RUNNING,
  or FAILED analysis is NOT evidence that a brand was absent. The service
  fails closed with ConflictError("Scan analysis is not completed.").
- A COMPLETED analysis with zero mentions is a TRUE measured zero
  (visibility = 0%, stability may be 100%).

Provider isolation:
- Every ProviderReliability value is calculated ONLY from that provider's
  PromptRuns. Provider numerators never inherit another provider's mentions.
- Provider round summaries, visibility ranges, valid round counts, and
  confidence levels are all provider-specific.

Confidence Level heuristic (repeat-reliability-v1):
- INSUFFICIENT: < 2 valid rounds, or coverage < 50%, or repeat sufficiency < 50%
- LOW: coverage < 75%, or repeat sufficiency < 75%, or mention stability < 60%
- HIGH: repeat_count >= 5, coverage >= 90%, repeat sufficiency >= 90%,
  mention stability >= 80%
- MEDIUM: otherwise

This is a PRODUCT EVIDENCE-QUALITY LABEL, NOT:
- statistical confidence
- probability of truth
- p-value
- 95% CI
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
    ScanAnalysisStatus,
    ScanType,
    TrackedEntityType,
)
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.analysis import (
    ANALYSIS_VERSION,
    EntityMention,
    ScanAnalysis,
    ScanEntitySnapshot,
)
from app.models.scan import PromptRun, Scan
from app.models.tracking import Prompt

# --- Methodology version (must bump if thresholds change) ---
CONFIDENCE_METHODOLOGY_VERSION = "repeat-reliability-v1"

# --- Threshold constants (single source of truth) ---
_THRESHOLD_COVERAGE_INSUFFICIENT = Decimal("50")
_THRESHOLD_COVERAGE_LOW = Decimal("75")
_THRESHOLD_COVERAGE_HIGH = Decimal("90")

_THRESHOLD_REPEAT_SUFFICIENCY_INSUFFICIENT = Decimal("50")
_THRESHOLD_REPEAT_SUFFICIENCY_LOW = Decimal("75")
_THRESHOLD_REPEAT_SUFFICIENCY_HIGH = Decimal("90")

_THRESHOLD_MENTION_STABILITY_LOW = Decimal("60")
_THRESHOLD_MENTION_STABILITY_HIGH = Decimal("80")

_THRESHOLD_HIGH_REPEAT_COUNT = 5
_THRESHOLD_MIN_VALID_ROUNDS = 2

_PRECISION = Decimal("0.0001")


class MeasurementConfidenceLevel(str):
    """Product evidence-quality label (NOT statistical confidence)."""

    INSUFFICIENT = "INSUFFICIENT"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


def _pct(numerator: int, denominator: int) -> Decimal | None:
    """Compute a percentage as Decimal, or None if denominator is 0."""
    if denominator == 0:
        return None
    return (Decimal(numerator) / Decimal(denominator) * Decimal(100)).quantize(
        _PRECISION, rounding=ROUND_HALF_UP
    )


def _entity_type_str(etype: object) -> str:
    """Safely convert a TrackedEntityType (enum or str) to its string value."""
    if hasattr(etype, "value"):
        return str(etype.value)
    return str(etype)


def _to_llm_provider(value: object) -> LLMProvider:
    """Safely convert a provider value (enum or str) to LLMProvider."""
    if isinstance(value, LLMProvider):
        return value
    return LLMProvider(str(value))


# ----------------------------------------------------------------------
# Result dataclasses
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RoundSummary:
    observation_index: int
    planned_observations: int
    successful_observations: int
    measurement_coverage: Decimal | None
    entity_visibility: dict[uuid.UUID, Decimal | None]


@dataclass(frozen=True)
class EntityReliability:
    entity_snapshot_id: uuid.UUID
    entity_type: str
    name: str
    domain: str
    overall_visibility_rate: Decimal | None
    planned_cells: int
    repeat_analyzable_cells: int
    stable_cells: int
    variable_cells: int
    insufficient_cells: int
    repeat_sufficiency: Decimal | None
    mention_stability: Decimal | None
    observed_visibility_min: Decimal | None
    observed_visibility_max: Decimal | None
    observed_visibility_range: Decimal | None
    confidence_level: str


@dataclass(frozen=True)
class ProviderReliability:
    provider: LLMProvider
    planned_observations: int
    successful_observations: int
    measurement_coverage: Decimal | None
    brand_visibility_rate: Decimal | None
    observed_visibility_min: Decimal | None
    observed_visibility_max: Decimal | None
    repeat_sufficiency: Decimal | None
    mention_stability: Decimal | None
    confidence_level: str


@dataclass(frozen=True)
class ConfidenceMetricsResult:
    scan_id: uuid.UUID
    baseline_scan_id: uuid.UUID | None
    repeat_count: int
    confidence_methodology_version: str
    scope: PromptType
    provider_filter: LLMProvider | None
    planned_observations: int
    successful_observations: int
    measurement_coverage: Decimal | None
    round_summaries: list[RoundSummary]
    entity_reliability: list[EntityReliability]
    provider_breakdown: list[ProviderReliability]
    overall_confidence_level: str


# ----------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------


class ConfidenceMetricsService:
    """Compute reliability metrics for a CONFIDENCE scan.

    No provider calls. No persistence of opaque percentages.
    Metrics are recomputed from immutable evidence on every request.

    Requires a COMPLETED ScanAnalysis. A missing/failed/pending analysis
    is NOT evidence of brand absence -- the service fails closed.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_metrics(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scan_id: uuid.UUID,
        prompt_type: PromptType = PromptType.NON_BRANDED,
        provider: LLMProvider | None = None,
    ) -> ConfidenceMetricsResult:
        scan = self._load_scoped_scan(workspace_id, project_id, scan_id)
        if scan.scan_type != ScanType.CONFIDENCE:
            raise ValidationError("Confidence metrics are only available for CONFIDENCE scans.")

        # Analysis readiness: require COMPLETED analysis.
        # A missing/failed/pending analysis is NOT evidence of brand absence.
        analysis = self._load_analysis(scan_id)
        self._require_completed_analysis(analysis)
        assert analysis is not None  # for type narrowing; _require_completed_analysis raises

        snapshots = self._load_snapshots(scan_id)
        runs_with_prompts = self._load_runs_with_prompts(scan_id)

        # Filter by scope (prompt_type + optional provider).
        scoped_runs = [
            (run, prompt)
            for run, prompt in runs_with_prompts
            if prompt.prompt_type == prompt_type and (provider is None or run.provider == provider)
        ]
        scoped_succeeded = [
            (run, prompt) for run, prompt in scoped_runs if run.status == PromptRunStatus.SUCCEEDED
        ]

        planned_count = len(scoped_runs)
        successful_count = len(scoped_succeeded)
        coverage = _pct(successful_count, planned_count)

        # Load mentions for succeeded runs (analysis is COMPLETED here).
        mentioned_run_ids_by_entity: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        succeeded_ids = {run.id for run, _ in scoped_succeeded}
        mentions = self._load_mentions(analysis.id, succeeded_ids)
        for mention in mentions:
            mentioned_run_ids_by_entity[mention.entity_snapshot_id].add(mention.prompt_run_id)

        # Round summaries.
        round_summaries = self._compute_round_summaries(
            scoped_runs, scoped_succeeded, snapshots, mentioned_run_ids_by_entity
        )

        # Entity reliability.
        entity_reliability = self._compute_entity_reliability(
            scoped_runs,
            scoped_succeeded,
            snapshots,
            mentioned_run_ids_by_entity,
            round_summaries,
            scan.repeat_count,
        )

        # Provider breakdown (provider-scoped).
        provider_breakdown = self._compute_provider_breakdown(
            scoped_runs,
            scoped_succeeded,
            snapshots,
            mentioned_run_ids_by_entity,
            scan.repeat_count,
        )

        # Overall confidence level.
        valid_rounds = [rs for rs in round_summaries if rs.successful_observations > 0]
        repeat_sufficiency = self._compute_repeat_sufficiency(scoped_runs, scoped_succeeded)
        mention_stability = self._compute_mention_stability(
            scoped_runs, scoped_succeeded, snapshots, mentioned_run_ids_by_entity
        )
        overall_level = classify_confidence_level(
            repeat_count=scan.repeat_count,
            valid_round_count=len(valid_rounds),
            measurement_coverage=coverage,
            repeat_sufficiency=repeat_sufficiency,
            mention_stability=mention_stability,
        )

        return ConfidenceMetricsResult(
            scan_id=scan_id,
            baseline_scan_id=scan.baseline_scan_id,
            repeat_count=scan.repeat_count,
            confidence_methodology_version=CONFIDENCE_METHODOLOGY_VERSION,
            scope=prompt_type,
            provider_filter=provider,
            planned_observations=planned_count,
            successful_observations=successful_count,
            measurement_coverage=coverage,
            round_summaries=round_summaries,
            entity_reliability=entity_reliability,
            provider_breakdown=provider_breakdown,
            overall_confidence_level=overall_level,
        )

    # ------------------------------------------------------------------
    # Analysis readiness
    # ------------------------------------------------------------------

    def _require_completed_analysis(self, analysis: ScanAnalysis | None) -> None:
        """Fail closed if analysis is missing or not COMPLETED.

        A missing, PENDING, RUNNING, or FAILED analysis is NOT evidence
        that a brand was absent. We must not silently populate zero-valued
        mention metrics.
        """
        if analysis is None:
            raise ConflictError("Scan analysis is not completed.")
        if analysis.status != ScanAnalysisStatus.COMPLETED:
            raise ConflictError("Scan analysis is not completed.")

    # ------------------------------------------------------------------
    # Round summaries
    # ------------------------------------------------------------------

    def _compute_round_summaries(
        self,
        scoped_runs: list[tuple[PromptRun, Prompt]],
        scoped_succeeded: list[tuple[PromptRun, Prompt]],
        snapshots: list[ScanEntitySnapshot],
        mentioned_run_ids_by_entity: dict[uuid.UUID, set[uuid.UUID]],
    ) -> list[RoundSummary]:
        rounds: list[RoundSummary] = []
        observation_indices = sorted({run.observation_index for run, _ in scoped_runs})
        for obs_idx in observation_indices:
            round_planned = [
                (run, prompt) for run, prompt in scoped_runs if run.observation_index == obs_idx
            ]
            round_succeeded = [
                (run, prompt)
                for run, prompt in scoped_succeeded
                if run.observation_index == obs_idx
            ]
            round_coverage = _pct(len(round_succeeded), len(round_planned))

            # Per-entity visibility for this round.
            entity_vis: dict[uuid.UUID, Decimal | None] = {}
            round_succeeded_ids = {run.id for run, _ in round_succeeded}
            for snap in snapshots:
                mentioned = mentioned_run_ids_by_entity.get(snap.id, set())
                mentioned_in_round = mentioned & round_succeeded_ids
                entity_vis[snap.id] = _pct(len(mentioned_in_round), len(round_succeeded))

            rounds.append(
                RoundSummary(
                    observation_index=obs_idx,
                    planned_observations=len(round_planned),
                    successful_observations=len(round_succeeded),
                    measurement_coverage=round_coverage,
                    entity_visibility=entity_vis,
                )
            )
        return rounds

    # ------------------------------------------------------------------
    # Entity reliability
    # ------------------------------------------------------------------

    def _compute_entity_reliability(
        self,
        scoped_runs: list[tuple[PromptRun, Prompt]],
        scoped_succeeded: list[tuple[PromptRun, Prompt]],
        snapshots: list[ScanEntitySnapshot],
        mentioned_run_ids_by_entity: dict[uuid.UUID, set[uuid.UUID]],
        round_summaries: list[RoundSummary],
        repeat_count: int,
    ) -> list[EntityReliability]:
        # Build cell map: (prompt_id, provider) -> list of succeeded run IDs.
        cell_succeeded: dict[tuple[uuid.UUID, LLMProvider], list[uuid.UUID]] = defaultdict(list)
        for run, _ in scoped_succeeded:
            cell_key = (run.prompt_id, _to_llm_provider(run.provider))
            cell_succeeded[cell_key].append(run.id)

        # All planned cells.
        planned_cells: set[tuple[uuid.UUID, LLMProvider]] = {
            (run.prompt_id, _to_llm_provider(run.provider)) for run, _ in scoped_runs
        }

        results: list[EntityReliability] = []
        for snap in snapshots:
            mentioned_runs = mentioned_run_ids_by_entity.get(snap.id, set())

            # Per-cell analysis.
            stable_cells = 0
            variable_cells = 0
            repeat_analyzable_cells = 0
            insufficient_cells = 0

            for cell_key in planned_cells:
                succeeded_ids = cell_succeeded.get(cell_key, [])
                successful_repeats = len(succeeded_ids)
                if successful_repeats >= 2:
                    repeat_analyzable_cells += 1
                    mentioned_repeats = sum(1 for rid in succeeded_ids if rid in mentioned_runs)
                    if mentioned_repeats == 0 or mentioned_repeats == successful_repeats:
                        stable_cells += 1
                    else:
                        variable_cells += 1
                else:
                    insufficient_cells += 1

            # Overall visibility rate.
            overall_vis = _pct(len(mentioned_runs), len(scoped_succeeded))

            # Repeat sufficiency.
            repeat_suff = _pct(repeat_analyzable_cells, len(planned_cells))

            # Mention stability.
            mention_stab = _pct(stable_cells, repeat_analyzable_cells)

            # Observed visibility range from rounds.
            round_vis_values: list[Decimal] = []
            for rs in round_summaries:
                vis = rs.entity_visibility.get(snap.id)
                if vis is not None:
                    round_vis_values.append(vis)

            if round_vis_values:
                vis_min = min(round_vis_values)
                vis_max = max(round_vis_values)
                vis_range = vis_max - vis_min
            else:
                vis_min = None
                vis_max = None
                vis_range = None

            # Confidence level for this entity.
            valid_rounds = [rs for rs in round_summaries if rs.successful_observations > 0]
            level = classify_confidence_level(
                repeat_count=repeat_count,
                valid_round_count=len(valid_rounds),
                measurement_coverage=_pct(len(scoped_succeeded), len(scoped_runs)),
                repeat_sufficiency=repeat_suff,
                mention_stability=mention_stab,
            )

            results.append(
                EntityReliability(
                    entity_snapshot_id=snap.id,
                    entity_type=_entity_type_str(snap.entity_type),
                    name=snap.name,
                    domain=snap.domain,
                    overall_visibility_rate=overall_vis,
                    planned_cells=len(planned_cells),
                    repeat_analyzable_cells=repeat_analyzable_cells,
                    stable_cells=stable_cells,
                    variable_cells=variable_cells,
                    insufficient_cells=insufficient_cells,
                    repeat_sufficiency=repeat_suff,
                    mention_stability=mention_stab,
                    observed_visibility_min=vis_min,
                    observed_visibility_max=vis_max,
                    observed_visibility_range=vis_range,
                    confidence_level=level,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Provider breakdown — fully provider-scoped
    # ------------------------------------------------------------------

    def _compute_provider_breakdown(
        self,
        scoped_runs: list[tuple[PromptRun, Prompt]],
        scoped_succeeded: list[tuple[PromptRun, Prompt]],
        snapshots: list[ScanEntitySnapshot],
        mentioned_run_ids_by_entity: dict[uuid.UUID, set[uuid.UUID]],
        repeat_count: int,
    ) -> list[ProviderReliability]:
        providers = sorted({_to_llm_provider(run.provider) for run, _ in scoped_runs})

        # Find brand snapshot using TrackedEntityType.BRAND (not PRIMARY_BRAND).
        brand_snap = self._find_brand_snapshot(snapshots)

        results: list[ProviderReliability] = []
        for prov in providers:
            prov_runs = [(r, p) for r, p in scoped_runs if _to_llm_provider(r.provider) == prov]
            prov_succeeded = [
                (r, p) for r, p in scoped_succeeded if _to_llm_provider(r.provider) == prov
            ]
            prov_coverage = _pct(len(prov_succeeded), len(prov_runs))

            # Brand visibility for this provider — numerator MUST intersect
            # with this provider's successful run IDs.
            brand_vis: Decimal | None = None
            if brand_snap is not None:
                prov_succeeded_ids = {r.id for r, _ in prov_succeeded}
                brand_mentioned = mentioned_run_ids_by_entity.get(brand_snap.id, set())
                brand_mentioned_in_provider = brand_mentioned & prov_succeeded_ids
                brand_vis = _pct(len(brand_mentioned_in_provider), len(prov_succeeded))

            # Per-provider cells: (prompt_id, provider) -> succeeded run IDs.
            # Since we're already scoped to one provider, the cell key is
            # effectively (prompt_id, prov).
            cell_succeeded: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
            for run, _ in prov_succeeded:
                cell_succeeded[run.prompt_id].append(run.id)

            planned_cells = {run.prompt_id for run, _ in prov_runs}

            repeat_analyzable = 0
            stable = 0
            for prompt_id in planned_cells:
                succeeded_ids = cell_succeeded.get(prompt_id, [])
                if len(succeeded_ids) >= 2:
                    repeat_analyzable += 1
                    if brand_snap is not None:
                        brand_mentioned = mentioned_run_ids_by_entity.get(brand_snap.id, set())
                        mentioned_repeats = sum(
                            1 for rid in succeeded_ids if rid in brand_mentioned
                        )
                        if mentioned_repeats == 0 or mentioned_repeats == len(succeeded_ids):
                            stable += 1
                    else:
                        stable += 1

            repeat_suff = _pct(repeat_analyzable, len(planned_cells))
            mention_stab = _pct(stable, repeat_analyzable)

            # Provider-specific round summaries for visibility range.
            # Only this provider's runs count toward round validity and
            # visibility.
            prov_round_vis_values: list[Decimal] = []
            prov_valid_rounds = 0
            observation_indices = sorted({run.observation_index for run, _ in prov_runs})
            for obs_idx in observation_indices:
                round_succeeded = [
                    (r, p) for r, p in prov_succeeded if r.observation_index == obs_idx
                ]
                if len(round_succeeded) > 0:
                    prov_valid_rounds += 1
                    if brand_snap is not None:
                        round_succeeded_ids = {r.id for r, _ in round_succeeded}
                        brand_mentioned = mentioned_run_ids_by_entity.get(brand_snap.id, set())
                        mentioned_in_round = brand_mentioned & round_succeeded_ids
                        vis = _pct(len(mentioned_in_round), len(round_succeeded))
                        if vis is not None:
                            prov_round_vis_values.append(vis)

            vis_min: Decimal | None = None
            vis_max: Decimal | None = None
            if prov_round_vis_values:
                vis_min = min(prov_round_vis_values)
                vis_max = max(prov_round_vis_values)

            # Provider-specific confidence level.
            level = classify_confidence_level(
                repeat_count=repeat_count,
                valid_round_count=prov_valid_rounds,
                measurement_coverage=prov_coverage,
                repeat_sufficiency=repeat_suff,
                mention_stability=mention_stab,
            )

            results.append(
                ProviderReliability(
                    provider=prov,
                    planned_observations=len(prov_runs),
                    successful_observations=len(prov_succeeded),
                    measurement_coverage=prov_coverage,
                    brand_visibility_rate=brand_vis,
                    observed_visibility_min=vis_min,
                    observed_visibility_max=vis_max,
                    repeat_sufficiency=repeat_suff,
                    mention_stability=mention_stab,
                    confidence_level=level,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Overall repeat sufficiency and mention stability
    # ------------------------------------------------------------------

    def _compute_repeat_sufficiency(
        self,
        scoped_runs: list[tuple[PromptRun, Prompt]],
        scoped_succeeded: list[tuple[PromptRun, Prompt]],
    ) -> Decimal | None:
        cell_succeeded: dict[tuple[uuid.UUID, LLMProvider], list[uuid.UUID]] = defaultdict(list)
        for run, _ in scoped_succeeded:
            cell_succeeded[(run.prompt_id, _to_llm_provider(run.provider))].append(run.id)

        planned_cells = {(run.prompt_id, _to_llm_provider(run.provider)) for run, _ in scoped_runs}
        repeat_analyzable = sum(
            1 for cell in planned_cells if len(cell_succeeded.get(cell, [])) >= 2
        )
        return _pct(repeat_analyzable, len(planned_cells))

    def _compute_mention_stability(
        self,
        scoped_runs: list[tuple[PromptRun, Prompt]],
        scoped_succeeded: list[tuple[PromptRun, Prompt]],
        snapshots: list[ScanEntitySnapshot],
        mentioned_run_ids_by_entity: dict[uuid.UUID, set[uuid.UUID]],
    ) -> Decimal | None:
        cell_succeeded: dict[tuple[uuid.UUID, LLMProvider], list[uuid.UUID]] = defaultdict(list)
        for run, _ in scoped_succeeded:
            cell_succeeded[(run.prompt_id, _to_llm_provider(run.provider))].append(run.id)

        planned_cells = {(run.prompt_id, _to_llm_provider(run.provider)) for run, _ in scoped_runs}

        repeat_analyzable_cells = 0
        stable_cells = 0
        for cell_key in planned_cells:
            succeeded_ids = cell_succeeded.get(cell_key, [])
            if len(succeeded_ids) >= 2:
                repeat_analyzable_cells += 1
                # A cell is stable if ALL entities agree across repeats.
                # For overall mention stability, we check if for every entity,
                # the mentions are all-yes or all-no.
                all_stable = True
                for snap in snapshots:
                    mentioned = mentioned_run_ids_by_entity.get(snap.id, set())
                    mentioned_repeats = sum(1 for rid in succeeded_ids if rid in mentioned)
                    if 0 < mentioned_repeats < len(succeeded_ids):
                        all_stable = False
                        break
                if all_stable:
                    stable_cells += 1

        return _pct(stable_cells, repeat_analyzable_cells)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_brand_snapshot(
        self, snapshots: list[ScanEntitySnapshot]
    ) -> ScanEntitySnapshot | None:
        """Find the BRAND snapshot using TrackedEntityType.BRAND.

        Robust to SQLAlchemy returning either the enum object or the
        string-backed value.
        """
        brand_str = TrackedEntityType.BRAND.value
        for s in snapshots:
            etype = _entity_type_str(s.entity_type)
            if etype == brand_str:
                return s
        return None

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
            .order_by(PromptRun.observation_index, PromptRun.created_at, PromptRun.id)
        ).all()
        # Unpack Row objects into plain tuples to match the return type.
        # C416 would rewrite this as list(rows), but Row is not a tuple
        # subclass, so the rewrite would change the return type.
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


# ----------------------------------------------------------------------
# Confidence level classifier — single source of truth for thresholds
# ----------------------------------------------------------------------


def classify_confidence_level(
    *,
    repeat_count: int,
    valid_round_count: int,
    measurement_coverage: Decimal | None,
    repeat_sufficiency: Decimal | None,
    mention_stability: Decimal | None,
) -> str:
    """Classify measurement confidence using deterministic v1 thresholds.

    This is a PRODUCT EVIDENCE-QUALITY LABEL, NOT statistical confidence.
    """
    # INSUFFICIENT
    if valid_round_count < _THRESHOLD_MIN_VALID_ROUNDS:
        return MeasurementConfidenceLevel.INSUFFICIENT
    if measurement_coverage is not None and measurement_coverage < _THRESHOLD_COVERAGE_INSUFFICIENT:
        return MeasurementConfidenceLevel.INSUFFICIENT
    if (
        repeat_sufficiency is not None
        and repeat_sufficiency < _THRESHOLD_REPEAT_SUFFICIENCY_INSUFFICIENT
    ):
        return MeasurementConfidenceLevel.INSUFFICIENT

    # LOW
    if measurement_coverage is not None and measurement_coverage < _THRESHOLD_COVERAGE_LOW:
        return MeasurementConfidenceLevel.LOW
    if repeat_sufficiency is not None and repeat_sufficiency < _THRESHOLD_REPEAT_SUFFICIENCY_LOW:
        return MeasurementConfidenceLevel.LOW
    if mention_stability is not None and mention_stability < _THRESHOLD_MENTION_STABILITY_LOW:
        return MeasurementConfidenceLevel.LOW

    # HIGH
    if (
        repeat_count >= _THRESHOLD_HIGH_REPEAT_COUNT
        and measurement_coverage is not None
        and measurement_coverage >= _THRESHOLD_COVERAGE_HIGH
        and repeat_sufficiency is not None
        and repeat_sufficiency >= _THRESHOLD_REPEAT_SUFFICIENCY_HIGH
        and mention_stability is not None
        and mention_stability >= _THRESHOLD_MENTION_STABILITY_HIGH
    ):
        return MeasurementConfidenceLevel.HIGH

    # MEDIUM
    return MeasurementConfidenceLevel.MEDIUM
