"""VerificationEvaluationService — deterministic before/after comparison.

Compares the frozen implementation baseline occurrence's metrics against
the verification scan's freshly computed metrics for the same Opportunity
scope. Produces a VerificationOutcome and persists it on the
OpportunityVerification record.

Key principles:
- Zero AI Checks, zero provider calls, zero UsageEvents.
- Uses CompetitorExplanationService for both baseline and verification
  metric computation, ensuring methodology consistency.
- Only RESOLVED may transition the parent Opportunity to VERIFIED.
- VERIFIED does NOT prove causation — it means the originally measured
  issue is no longer present under the verification methodology.
- INCONCLUSIVE outcomes preserve the verification record as historical
  evidence without changing the Opportunity status.

Outcome decision tree:
1. If verification analysis is not COMPLETED → INCONCLUSIVE
   (ANALYSIS_NOT_COMPLETED).
2. If verification measurement coverage < MIN_VERIFICATION_COVERAGE_PCT
   → INCONCLUSIVE (INSUFFICIENT_COVERAGE).
3. If verification has zero successful observations → INCONCLUSIVE
   (NO_SUCCESSFUL_OBSERVATIONS).
4. For OWNED_CITATION_GAP: if citation_eligible_observations <
   MIN_CITATION_ELIGIBLE_OBSERVATIONS → INCONCLUSIVE
   (INSUFFICIENT_CITATION_EVIDENCE).
5. Compute the verification metric value and compare to the baseline:
   - If verification value < resolution_threshold → RESOLVED.
   - Else if (baseline - verification) >= meaningful_improvement_threshold
     → IMPROVED.
   - Else if (verification - baseline) >= meaningful_improvement_threshold
     → REGRESSED.
   - Else → NOT_IMPROVED.
6. For PROMPT_COMPETITOR_GAP: competitor_only_count == 0 → RESOLVED.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    LLMProvider,
    OpportunityType,
    PromptType,
    ScanAnalysisStatus,
    ScanStatus,
    ScanType,
    VerificationOutcome,
    VerificationReasonCode,
)
from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.verification_engine import (
    MIN_CITATION_ELIGIBLE_OBSERVATIONS,
    MIN_CITATION_IMPROVEMENT_PP,
    MIN_DISCOVERY_VISIBILITY_GAP_PP,
    MIN_OWNED_CITATION_GAP_PP,
    MIN_PROMPT_GAP_IMPROVEMENT_PP,
    MIN_PROVIDER_VISIBILITY_GAP_PP,
    MIN_VERIFICATION_COVERAGE_PCT,
    MIN_VISIBILITY_IMPROVEMENT_PP,
)
from app.models.analysis import ScanAnalysis, ScanEntitySnapshot
from app.models.opportunity import (
    Opportunity,
    OpportunityOccurrence,
    OpportunityVerification,
)
from app.models.scan import Scan
from app.services.competitor_explanation_service import (
    CompetitorExplanation,
    CompetitorExplanationService,
)
from app.services.opportunity_workflow_service import OpportunityWorkflowService

logger = get_logger("app.verification_evaluation")


@dataclass(frozen=True)
class VerificationEvaluationResult:
    """Result of a verification evaluation."""

    verification_id: uuid.UUID
    opportunity_id: uuid.UUID
    outcome: VerificationOutcome
    reason_code: VerificationReasonCode | None
    evaluation_message: str
    metric_name: str
    baseline_value: Decimal | None
    verification_value: Decimal | None
    delta_value: Decimal | None
    baseline_brand_value: Decimal | None
    verification_brand_value: Decimal | None
    baseline_coverage: Decimal | None
    verification_coverage: Decimal | None
    resolution_threshold: Decimal | None
    meaningful_improvement_threshold: Decimal | None
    opportunity_status_after: str


class VerificationEvaluationService:
    """Deterministic before/after comparison for Opportunity verification."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._explanation_service = CompetitorExplanationService(session)
        self._workflow_service = OpportunityWorkflowService(session)

    def evaluate(self, verification_id: uuid.UUID) -> VerificationEvaluationResult:
        """Evaluate a verification comparison.

        Loads the OpportunityVerification, the verification scan, computes
        the verification metrics, compares to the frozen baseline, and
        persists the outcome.  If the outcome is RESOLVED, transitions
        the parent Opportunity to VERIFIED.

        Raises NotFoundError if the verification record doesn't exist.
        Raises ValidationError if the verification scan is not terminal
        or not a VERIFICATION scan.
        """
        verification = self._load_verification_for_update(verification_id)
        if verification.outcome != VerificationOutcome.PENDING:
            # Already evaluated — return the existing result.
            return self._build_result_from_existing(verification)

        opportunity = self._load_opportunity(verification.opportunity_id)
        baseline_occurrence = self._session.get(
            OpportunityOccurrence, verification.baseline_occurrence_id
        )
        if baseline_occurrence is None:
            return self._persist_inconclusive(
                verification,
                VerificationReasonCode.BASELINE_EVIDENCE_UNAVAILABLE,
                "Frozen baseline occurrence not found.",
            )

        verification_scan = self._session.get(Scan, verification.verification_scan_id)
        if verification_scan is None:
            return self._persist_inconclusive(
                verification,
                VerificationReasonCode.BASELINE_EVIDENCE_UNAVAILABLE,
                "Verification scan not found.",
            )
        if verification_scan.scan_type != ScanType.VERIFICATION:
            raise ValidationError("Verification scan is not a VERIFICATION scan.")
        if verification_scan.status not in (ScanStatus.COMPLETED, ScanStatus.PARTIAL):
            raise ValidationError("Verification scan must be COMPLETED or PARTIAL to evaluate.")

        # Check the verification analysis is COMPLETED.
        verification_analysis = self._load_analysis(verification_scan.id)
        if (
            verification_analysis is None
            or verification_analysis.status != ScanAnalysisStatus.COMPLETED
        ):
            return self._persist_inconclusive(
                verification,
                VerificationReasonCode.ANALYSIS_NOT_COMPLETED,
                "Verification scan analysis is not completed.",
            )

        # Compute the verification explanation.
        verification_explanation = self._compute_explanation(
            verification_scan.workspace_id,
            verification_scan.project_id,
            verification_scan.id,
            opportunity,
            baseline_occurrence,
        )

        # Coverage gate.
        verification_coverage = verification_explanation.measurement_coverage
        if verification_coverage is None or verification_coverage < MIN_VERIFICATION_COVERAGE_PCT:
            return self._persist_inconclusive(
                verification,
                VerificationReasonCode.INSUFFICIENT_COVERAGE,
                f"Verification measurement coverage ({verification_coverage}) "
                f"is below the minimum threshold ({MIN_VERIFICATION_COVERAGE_PCT}%).",
                verification_coverage=verification_coverage,
            )

        # Zero successful observations gate.
        if verification_explanation.successful_observations == 0:
            return self._persist_inconclusive(
                verification,
                VerificationReasonCode.NO_SUCCESSFUL_OBSERVATIONS,
                "Verification scan has no successful observations.",
                verification_coverage=verification_coverage,
            )

        # Compute the baseline explanation for the same scope.
        baseline_scan = self._session.get(Scan, verification.baseline_scan_id)
        if baseline_scan is None:
            return self._persist_inconclusive(
                verification,
                VerificationReasonCode.BASELINE_EVIDENCE_UNAVAILABLE,
                "Baseline scan not found.",
                verification_coverage=verification_coverage,
            )

        baseline_explanation = self._compute_explanation(
            baseline_scan.workspace_id,
            baseline_scan.project_id,
            baseline_scan.id,
            opportunity,
            baseline_occurrence,
        )

        # Determine the metric and thresholds based on opportunity type.
        metric_name, resolution_threshold, improvement_threshold = self._resolve_metric_config(
            opportunity, baseline_explanation
        )

        # Citation-specific gate.
        if (
            opportunity.opportunity_type == OpportunityType.OWNED_CITATION_GAP
            and verification_explanation.citation_eligible_observations
            < MIN_CITATION_ELIGIBLE_OBSERVATIONS
        ):
            return self._persist_inconclusive(
                verification,
                VerificationReasonCode.INSUFFICIENT_CITATION_EVIDENCE,
                f"Verification citation-eligible observations "
                f"({verification_explanation.citation_eligible_observations}) "
                f"is below the minimum ({MIN_CITATION_ELIGIBLE_OBSERVATIONS}).",
                verification_coverage=verification_coverage,
            )

        # Extract the baseline and verification metric values.
        baseline_value = self._extract_metric_value(
            metric_name, baseline_explanation, opportunity, baseline_occurrence
        )
        verification_value = self._extract_metric_value(
            metric_name, verification_explanation, opportunity, baseline_occurrence
        )

        baseline_brand_value = self._extract_brand_value(metric_name, baseline_explanation)
        verification_brand_value = self._extract_brand_value(metric_name, verification_explanation)

        # Compute the delta (baseline - verification; positive = improvement).
        delta = self._compute_delta(baseline_value, verification_value)

        # Decide the outcome.
        outcome, message = self._decide_outcome(
            metric_name,
            baseline_value,
            verification_value,
            delta,
            resolution_threshold,
            improvement_threshold,
            opportunity.opportunity_type,
        )

        # Persist the evaluation.
        now = datetime.now(UTC)
        verification.outcome = outcome
        verification.reason_code = (
            None
            if outcome != VerificationOutcome.INCONCLUSIVE
            else (self._resolve_reason_code(message))
        )
        verification.evaluation_message = message
        verification.metric_name = metric_name
        verification.baseline_value = baseline_value
        verification.verification_value = verification_value
        verification.delta_value = delta
        verification.baseline_brand_value = baseline_brand_value
        verification.verification_brand_value = verification_brand_value
        verification.baseline_coverage = baseline_explanation.measurement_coverage
        verification.verification_coverage = verification_coverage
        verification.resolution_threshold = resolution_threshold
        verification.meaningful_improvement_threshold = improvement_threshold
        verification.evaluated_at = now

        # Transition the Opportunity to VERIFIED if RESOLVED.
        status_after = opportunity.status
        if outcome == VerificationOutcome.RESOLVED:
            status_after = self._workflow_service.mark_verified_from_verification(
                verification.workspace_id,
                verification.project_id,
                verification.opportunity_id,
                verification.id,
            )

        self._session.commit()
        return VerificationEvaluationResult(
            verification_id=verification.id,
            opportunity_id=verification.opportunity_id,
            outcome=outcome,
            reason_code=verification.reason_code,
            evaluation_message=message,
            metric_name=metric_name,
            baseline_value=baseline_value,
            verification_value=verification_value,
            delta_value=delta,
            baseline_brand_value=baseline_brand_value,
            verification_brand_value=verification_brand_value,
            baseline_coverage=baseline_explanation.measurement_coverage,
            verification_coverage=verification_coverage,
            resolution_threshold=resolution_threshold,
            meaningful_improvement_threshold=improvement_threshold,
            opportunity_status_after=status_after.value
            if hasattr(status_after, "value")
            else str(status_after),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_verification_for_update(self, verification_id: uuid.UUID) -> OpportunityVerification:
        verification = self._session.execute(
            select(OpportunityVerification)
            .where(OpportunityVerification.id == verification_id)
            .with_for_update()
        ).scalar_one_or_none()
        if verification is None:
            raise NotFoundError("Verification record not found.")
        return verification

    def _load_opportunity(self, opportunity_id: uuid.UUID) -> Opportunity:
        opp = self._session.get(Opportunity, opportunity_id)
        if opp is None:
            raise NotFoundError("Opportunity not found.")
        return opp

    def _load_analysis(self, scan_id: uuid.UUID) -> ScanAnalysis | None:
        from app.repositories.analysis_repository import ScanAnalysisRepository

        return ScanAnalysisRepository(self._session).get_by_scan_and_version(
            scan_id, "deterministic-entity-v1"
        )

    def _compute_explanation(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scan_id: uuid.UUID,
        opportunity: Opportunity,
        baseline_occurrence: OpportunityOccurrence,
    ) -> CompetitorExplanation:
        """Compute the CompetitorExplanation for the scan, scoped to the
        Opportunity's competitor and provider.

        For PROVIDER_VISIBILITY_GAP, the provider filter is applied.
        For other types, no provider filter.
        """
        # Find the competitor snapshot for this scan.
        snapshots = list(
            self._session.execute(
                select(ScanEntitySnapshot).where(
                    ScanEntitySnapshot.scan_id == scan_id,
                    ScanEntitySnapshot.entity_key == opportunity.competitor_entity_key,
                )
            ).scalars()
        )
        if not snapshots:
            raise ValidationError(
                f"Competitor snapshot '{opportunity.competitor_entity_key}' "
                f"not found in scan {scan_id}."
            )
        competitor_snapshot_id = snapshots[0].id

        provider_filter: LLMProvider | None = None
        if opportunity.opportunity_type == OpportunityType.PROVIDER_VISIBILITY_GAP:
            provider_filter = opportunity.provider

        return self._explanation_service.get_explanation(
            workspace_id,
            project_id,
            scan_id,
            competitor_snapshot_id,
            prompt_type=PromptType.NON_BRANDED,
            provider=provider_filter,
        )

    def _resolve_metric_config(
        self,
        opportunity: Opportunity,
        baseline_explanation: CompetitorExplanation,
    ) -> tuple[str, Decimal, Decimal]:
        """Resolve the metric name, resolution threshold, and meaningful
        improvement threshold based on the Opportunity type.
        """
        opp_type = opportunity.opportunity_type
        if opp_type == OpportunityType.DISCOVERY_VISIBILITY_GAP:
            return (
                "visibility_gap_pp",
                MIN_DISCOVERY_VISIBILITY_GAP_PP,
                MIN_VISIBILITY_IMPROVEMENT_PP,
            )
        if opp_type == OpportunityType.PROVIDER_VISIBILITY_GAP:
            return (
                "visibility_gap_pp",
                MIN_PROVIDER_VISIBILITY_GAP_PP,
                MIN_VISIBILITY_IMPROVEMENT_PP,
            )
        if opp_type == OpportunityType.OWNED_CITATION_GAP:
            return (
                "citation_gap_pp",
                MIN_OWNED_CITATION_GAP_PP,
                MIN_CITATION_IMPROVEMENT_PP,
            )
        if opp_type == OpportunityType.PROMPT_COMPETITOR_GAP:
            return (
                "competitor_only_count",
                Decimal(1),  # resolution: competitor_only_count == 0
                MIN_PROMPT_GAP_IMPROVEMENT_PP,
            )
        # Fallback (should not happen).
        return (
            "visibility_gap_pp",
            MIN_DISCOVERY_VISIBILITY_GAP_PP,
            MIN_VISIBILITY_IMPROVEMENT_PP,
        )

    def _extract_metric_value(
        self,
        metric_name: str,
        explanation: CompetitorExplanation,
        opportunity: Opportunity,
        baseline_occurrence: OpportunityOccurrence,
    ) -> Decimal | None:
        """Extract the metric value from the explanation.

        For visibility_gap_pp and citation_gap_pp, uses the explanation's
        gap. For competitor_only_count (PROMPT_COMPETITOR_GAP), uses the
        overlap matrix's competitor_only_runs.
        """
        if metric_name == "visibility_gap_pp":
            return explanation.visibility_gap_pp
        if metric_name == "citation_gap_pp":
            return explanation.citation_gap_pp
        if metric_name == "competitor_only_count":
            return Decimal(explanation.overlap.competitor_only_runs)
        return None

    def _extract_brand_value(
        self, metric_name: str, explanation: CompetitorExplanation
    ) -> Decimal | None:
        """Extract the brand-side metric value for transparency."""
        if metric_name == "visibility_gap_pp":
            return explanation.brand_visibility_rate
        if metric_name == "citation_gap_pp":
            return explanation.brand_owned_citation_rate
        if metric_name == "competitor_only_count":
            return explanation.brand_visibility_rate
        return None

    def _compute_delta(
        self, baseline: Decimal | None, verification: Decimal | None
    ) -> Decimal | None:
        """Compute delta = baseline - verification (positive = improvement)."""
        if baseline is None or verification is None:
            return None
        return baseline - verification

    def _decide_outcome(
        self,
        metric_name: str,
        baseline_value: Decimal | None,
        verification_value: Decimal | None,
        delta: Decimal | None,
        resolution_threshold: Decimal,
        improvement_threshold: Decimal,
        opp_type: OpportunityType,
    ) -> tuple[VerificationOutcome, str]:
        """Decide the verification outcome.

        For gap-based metrics (visibility_gap_pp, citation_gap_pp):
        - RESOLVED: verification_value < resolution_threshold
        - IMPROVED: delta >= improvement_threshold (and not RESOLVED)
        - REGRESSED: -delta >= improvement_threshold
        - NOT_IMPROVED: otherwise

        For competitor_only_count (PROMPT_COMPETITOR_GAP):
        - RESOLVED: verification_value == 0
        - IMPROVED: delta >= improvement_threshold (count dropped)
        - REGRESSED: -delta >= improvement_threshold (count grew)
        - NOT_IMPROVED: otherwise
        """
        if verification_value is None:
            return (
                VerificationOutcome.INCONCLUSIVE,
                "Verification metric value is unavailable.",
            )

        # Resolution check.
        if metric_name == "competitor_only_count":
            if verification_value == 0:
                return (
                    VerificationOutcome.RESOLVED,
                    "Competitor-only observations dropped to zero for this prompt.",
                )
        else:
            if verification_value < resolution_threshold:
                return (
                    VerificationOutcome.RESOLVED,
                    f"Verification {metric_name} ({verification_value}) is below "
                    f"the resolution threshold ({resolution_threshold}).",
                )

        if delta is None or baseline_value is None:
            return (
                VerificationOutcome.INCONCLUSIVE,
                "Cannot compute delta — baseline or verification value is missing.",
            )

        # Improvement / regression check.
        if delta >= improvement_threshold:
            return (
                VerificationOutcome.IMPROVED,
                f"{metric_name} improved by {delta} (baseline={baseline_value}, "
                f"verification={verification_value}); still above the resolution "
                f"threshold ({resolution_threshold}).",
            )
        if (-delta) >= improvement_threshold:
            return (
                VerificationOutcome.REGRESSED,
                f"{metric_name} regressed by {-delta} (baseline={baseline_value}, "
                f"verification={verification_value}).",
            )
        return (
            VerificationOutcome.NOT_IMPROVED,
            f"{metric_name} changed by {delta} (baseline={baseline_value}, "
            f"verification={verification_value}); below the meaningful "
            f"improvement threshold ({improvement_threshold}).",
        )

    def _resolve_reason_code(self, message: str) -> VerificationReasonCode:
        """Map an INCONCLUSIVE message to a bounded reason code."""
        lower = message.lower()
        if "coverage" in lower:
            return VerificationReasonCode.INSUFFICIENT_COVERAGE
        if "analysis" in lower:
            return VerificationReasonCode.ANALYSIS_NOT_COMPLETED
        if "no successful observations" in lower:
            return VerificationReasonCode.NO_SUCCESSFUL_OBSERVATIONS
        if "citation" in lower:
            return VerificationReasonCode.INSUFFICIENT_CITATION_EVIDENCE
        return VerificationReasonCode.BASELINE_EVIDENCE_UNAVAILABLE

    def _persist_inconclusive(
        self,
        verification: OpportunityVerification,
        reason_code: VerificationReasonCode,
        message: str,
        *,
        verification_coverage: Decimal | None = None,
    ) -> VerificationEvaluationResult:
        """Persist an INCONCLUSIVE outcome and return the result."""
        now = datetime.now(UTC)
        verification.outcome = VerificationOutcome.INCONCLUSIVE
        verification.reason_code = reason_code
        verification.evaluation_message = message
        verification.evaluated_at = now
        if verification_coverage is not None:
            verification.verification_coverage = verification_coverage
        self._session.commit()
        return VerificationEvaluationResult(
            verification_id=verification.id,
            opportunity_id=verification.opportunity_id,
            outcome=VerificationOutcome.INCONCLUSIVE,
            reason_code=reason_code,
            evaluation_message=message,
            metric_name=verification.metric_name,
            baseline_value=verification.baseline_value,
            verification_value=verification.verification_value,
            delta_value=verification.delta_value,
            baseline_brand_value=verification.baseline_brand_value,
            verification_brand_value=verification.verification_brand_value,
            baseline_coverage=verification.baseline_coverage,
            verification_coverage=verification_coverage,
            resolution_threshold=verification.resolution_threshold,
            meaningful_improvement_threshold=verification.meaningful_improvement_threshold,
            opportunity_status_after="",
        )

    def _build_result_from_existing(
        self, verification: OpportunityVerification
    ) -> VerificationEvaluationResult:
        """Build a result from an already-evaluated verification record."""
        opp = self._session.get(Opportunity, verification.opportunity_id)
        status_after = (
            (opp.status.value if hasattr(opp.status, "value") else str(opp.status))
            if opp is not None
            else ""
        )
        return VerificationEvaluationResult(
            verification_id=verification.id,
            opportunity_id=verification.opportunity_id,
            outcome=verification.outcome,
            reason_code=verification.reason_code,
            evaluation_message=verification.evaluation_message or "",
            metric_name=verification.metric_name,
            baseline_value=verification.baseline_value,
            verification_value=verification.verification_value,
            delta_value=verification.delta_value,
            baseline_brand_value=verification.baseline_brand_value,
            verification_brand_value=verification.verification_brand_value,
            baseline_coverage=verification.baseline_coverage,
            verification_coverage=verification.verification_coverage,
            resolution_threshold=verification.resolution_threshold,
            meaningful_improvement_threshold=verification.meaningful_improvement_threshold,
            opportunity_status_after=status_after,
        )
