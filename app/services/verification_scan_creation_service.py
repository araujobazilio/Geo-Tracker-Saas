"""Verification Scan creation — clone a baseline STANDARD scan's methodology
for Opportunity verification (Phase 10).

A Verification Scan repeats the SAME immutable measurement cells (Prompt x
Provider) as the frozen implementation baseline STANDARD scan, exactly
once (repeat_count=1). The resulting evidence is compared deterministically
against the baseline by VerificationEvaluationService.

Key invariants:
- The Opportunity must be in IMPLEMENTED status with a frozen
  implementation_baseline_occurrence_id.
- The baseline scan (referenced by the frozen occurrence) must be a
  COMPLETED or PARTIAL STANDARD scan with successful runs, zero
  unresolved PromptRuns, entity snapshots, and a full immutable
  PromptRun plan.
- The baseline's exact prompt_set_id, prompt IDs, provider surfaces,
  execution modes, requested models, and entity snapshots are cloned.
- Current project configuration must NOT alter the cloned methodology.
- Current entitlements and provider configuration must still allow ALL
  baseline providers.
- The verification_scans entitlement must be enabled.
- Quota is reserved for prompt_count x provider_count AI Checks
  (repeat_count=1) before dispatch.
- One active verification per Opportunity implementation cycle is
  enforced via the unique (workspace_id, idempotency_key) constraint
  and the unique verification_scan_id constraint on
  OpportunityVerification.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.enums import (
    LLMProvider,
    OpportunityStatus,
    ProjectStatus,
    PromptRunStatus,
    ProviderExecutionMode,
    ScanAnalysisStatus,
    ScanStatus,
    ScanType,
)
from app.core.exceptions import (
    ConflictError,
    EntitlementDeniedError,
    InfrastructureError,
    NotFoundError,
    PricingRuleNotFoundError,
    QuotaExceededError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.verification_engine import VERIFICATION_METHODOLOGY_VERSION
from app.models.analysis import ScanEntitySnapshot
from app.models.opportunity import (
    Opportunity,
    OpportunityOccurrence,
    OpportunityVerification,
)
from app.models.scan import PromptRun, Scan
from app.models.tracking import Prompt
from app.providers.registry import ProviderRegistry
from app.repositories.analysis_repository import (
    ScanAnalysisRepository,
    ScanEntitySnapshotRepository,
)
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import PromptRunRepository, ScanRepository
from app.repositories.tracking_repository import (
    ProjectProviderRepository,
    PromptRepository,
)
from app.services.audit_service import AuditService
from app.services.confidence_scan_creation_service import (
    BaselineProviderTarget,
    ScanCreationServiceIdempotencyHelper,
)
from app.services.entitlement_service import EntitlementService
from app.services.pricing_service import PricingService
from app.services.quota_service import QuotaService
from app.services.scanning.dispatcher import ScanDispatcher
from app.services.scanning.policy import PROVIDER_ORDER

logger = get_logger("app.verification_scan_creation")


@dataclass(frozen=True)
class VerificationScanCreationResult:
    scan: Scan
    verification: OpportunityVerification
    created: bool
    dispatched: bool


_BASELINE_ELIGIBLE_STATUSES = (ScanStatus.COMPLETED, ScanStatus.PARTIAL)


class VerificationScanCreationService:
    """Create a VERIFICATION scan from an Opportunity's frozen baseline."""

    def __init__(
        self,
        session: Session,
        dispatcher: ScanDispatcher,
        *,
        settings: Settings | None = None,
        registry: ProviderRegistry | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._session = session
        self._dispatcher = dispatcher
        self._settings = settings or get_settings()
        self._registry = registry or ProviderRegistry()
        self._audit = audit_service
        self._projects = ProjectRepository(session)
        self._scans = ScanRepository(session)
        self._runs = PromptRunRepository(session)
        self._snapshots = ScanEntitySnapshotRepository(session)
        self._analyses = ScanAnalysisRepository(session)
        self._prompts = PromptRepository(session)
        self._project_providers = ProjectProviderRepository(session)
        self._entitlements = EntitlementService(session)
        self._pricing = PricingService(session)
        self._quota = QuotaService(session, audit_service=audit_service)

    def create_verification_scan(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        opportunity_id: uuid.UUID,
        requested_by_user_id: uuid.UUID | None,
        idempotency_key: str,
    ) -> VerificationScanCreationResult:
        """Create a VERIFICATION scan for an IMPLEMENTED Opportunity.

        Returns the new Scan and the OpportunityVerification record.
        """
        key = ScanCreationServiceIdempotencyHelper.normalize(idempotency_key)

        # Idempotency: existing verification with same key.
        existing_verification = self._find_existing_verification(workspace_id, key)
        if existing_verification is not None:
            self._validate_existing_verification(existing_verification, project_id, opportunity_id)
            existing_scan = self._scans.get_by_id(existing_verification.verification_scan_id)
            if existing_scan is None:
                self._session.rollback()
                raise InfrastructureError("Verification scan missing.")
            self._session.commit()
            return self._resume_dispatch_if_needed(existing_scan, existing_verification)

        # Load and lock the Opportunity.
        opp = self._load_opportunity_for_update(workspace_id, project_id, opportunity_id)
        if opp.status != OpportunityStatus.IMPLEMENTED:
            self._session.rollback()
            raise ValidationError(
                "Opportunity must be in IMPLEMENTED status to create a verification scan."
            )
        if opp.implementation_baseline_occurrence_id is None:
            self._session.rollback()
            raise ValidationError("Opportunity has no frozen implementation baseline occurrence.")

        # Load the frozen baseline occurrence + baseline scan.
        baseline_occurrence = self._session.get(
            OpportunityOccurrence, opp.implementation_baseline_occurrence_id
        )
        if baseline_occurrence is None:
            self._session.rollback()
            raise ValidationError("Frozen baseline occurrence not found.")

        baseline_scan_id = baseline_occurrence.scan_id
        baseline = self._load_and_validate_baseline(workspace_id, project_id, baseline_scan_id)

        # Validate the project is still ACTIVE.
        project = self._projects.get_in_workspace_for_update(project_id, workspace_id)
        if project is None:
            self._session.rollback()
            raise NotFoundError("Project not found.")
        if project.status != ProjectStatus.ACTIVE:
            self._session.rollback()
            raise ConflictError("Project must be ACTIVE to start a scan.")

        # Entitlement: verification_scans feature must be enabled.
        try:
            self._entitlements.require_feature(workspace_id, "verification_scans")
        except EntitlementDeniedError:
            self._session.rollback()
            raise

        # Validate all baseline providers are still allowed + configured.
        baseline_targets = self._validate_baseline_providers(workspace_id, project_id, baseline)

        # Pricing preflight.
        self._pricing_preflight(baseline_targets)

        # Load baseline prompts.
        baseline_prompts = self._load_baseline_prompts(baseline)

        # Load baseline entity snapshots.
        baseline_snapshots = self._snapshots.list_by_scan(baseline.id)
        if not baseline_snapshots:
            self._session.rollback()
            raise ConflictError("Baseline scan has no entity snapshots.")

        # Compute planned AI checks (repeat_count=1).
        prompt_count = len(baseline_prompts)
        provider_count = len(baseline_targets)
        planned_ai_checks = prompt_count * provider_count

        try:
            scan = Scan(
                workspace_id=workspace_id,
                project_id=project.id,
                prompt_set_id=baseline.prompt_set_id,
                scan_type=ScanType.VERIFICATION,
                status=ScanStatus.PENDING,
                requested_by_user_id=requested_by_user_id,
                idempotency_key=key,
                prompt_count=prompt_count,
                provider_count=provider_count,
                planned_ai_checks=planned_ai_checks,
                successful_runs=0,
                failed_runs=0,
                repeat_count=1,
                baseline_scan_id=baseline.id,
            )
            self._scans.create(scan)

            planned_runs = self._create_verification_runs(
                scan.id, baseline_prompts, baseline_targets
            )
            self._runs.create_batch(planned_runs)

            self._clone_entity_snapshots(scan.id, baseline_snapshots)

            # Create the OpportunityVerification record (PENDING).
            verification = OpportunityVerification(
                workspace_id=workspace_id,
                project_id=project.id,
                opportunity_id=opp.id,
                baseline_occurrence_id=baseline_occurrence.id,
                baseline_scan_id=baseline.id,
                verification_scan_id=scan.id,
                idempotency_key=key,
                verification_methodology_version=VERIFICATION_METHODOLOGY_VERSION,
                outcome="PENDING",
                metric_name=self._derive_metric_name(opp),
            )
            self._session.add(verification)

            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing_verification = self._find_existing_verification(workspace_id, key)
            if existing_verification is None:
                raise
            self._validate_existing_verification(existing_verification, project_id, opportunity_id)
            existing_scan = self._scans.get_by_id(existing_verification.verification_scan_id)
            if existing_scan is None:
                raise InfrastructureError(
                    "Verification scan missing after IntegrityError."
                ) from None
            return self._resume_dispatch_if_needed(existing_scan, existing_verification)
        except Exception:
            self._session.rollback()
            raise

        # Reserve quota.
        try:
            reservation = self._quota.reserve_ai_checks(
                workspace_id=workspace_id,
                requested_checks=scan.planned_ai_checks,
                idempotency_key=f"scan:{scan.id}",
                user_id=requested_by_user_id,
                project_id=project_id,
                ttl_seconds=self._settings.scan_reservation_ttl_seconds,
            )
        except QuotaExceededError as exc:
            self._mark_quota_failed(scan.id, str(exc))
            raise

        attached_scan = self._scans.get_by_id(scan.id)
        if attached_scan is None:
            raise InfrastructureError("Scan disappeared after quota reservation.")
        attached_scan.quota_reservation_id = reservation.id
        self._session.commit()
        self._record_audit("SCAN_CREATED", attached_scan)
        return self._dispatch(attached_scan, verification, created=True)

    # ------------------------------------------------------------------
    # Opportunity loading
    # ------------------------------------------------------------------

    def _load_opportunity_for_update(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        opportunity_id: uuid.UUID,
    ) -> Opportunity:
        opp = self._session.execute(
            select(Opportunity)
            .where(
                Opportunity.id == opportunity_id,
                Opportunity.workspace_id == workspace_id,
                Opportunity.project_id == project_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if opp is None:
            self._session.rollback()
            raise NotFoundError("Opportunity not found.")
        return opp

    def _derive_metric_name(self, opp: Opportunity) -> str:
        """Derive the metric name for the verification comparison."""
        return {
            "DISCOVERY_VISIBILITY_GAP": "visibility_gap_pp",
            "PROVIDER_VISIBILITY_GAP": "visibility_gap_pp",
            "OWNED_CITATION_GAP": "citation_gap_pp",
            "PROMPT_COMPETITOR_GAP": "competitor_only_count",
        }.get(
            opp.opportunity_type.value
            if hasattr(opp.opportunity_type, "value")
            else str(opp.opportunity_type),
            "visibility_gap_pp",
        )

    # ------------------------------------------------------------------
    # Baseline validation
    # ------------------------------------------------------------------

    def _load_and_validate_baseline(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        baseline_scan_id: uuid.UUID,
    ) -> Scan:
        baseline = self._scans.get_scoped(workspace_id, project_id, baseline_scan_id)
        if baseline is None:
            self._session.rollback()
            raise NotFoundError("Baseline scan not found.")
        if baseline.scan_type != ScanType.STANDARD:
            self._session.rollback()
            raise ValidationError("Baseline scan must be a STANDARD scan.")
        if baseline.status not in _BASELINE_ELIGIBLE_STATUSES:
            self._session.rollback()
            raise ValidationError("Baseline scan must be COMPLETED or PARTIAL.")
        if baseline.successful_runs == 0:
            self._session.rollback()
            raise ValidationError(
                "Baseline scan has no successful runs; cannot repeat measurement."
            )
        _succeeded, _failed, unresolved = self._runs.terminal_counts(baseline.id)
        if unresolved > 0:
            self._session.rollback()
            raise ValidationError("Baseline scan has unresolved PromptRuns.")
        snapshot_count = self._snapshots.count_by_scan(baseline.id)
        if snapshot_count == 0:
            self._session.rollback()
            raise ValidationError("Baseline scan has no entity snapshots.")
        run_count = self._runs.count_by_scan(baseline.id)
        if run_count != baseline.planned_ai_checks:
            self._session.rollback()
            raise ValidationError("Baseline scan PromptRun plan is incomplete.")
        # Validate the baseline analysis is COMPLETED.
        analysis = self._analyses.get_by_scan_and_version(baseline.id, "deterministic-entity-v1")
        if analysis is None or analysis.status != ScanAnalysisStatus.COMPLETED:
            self._session.rollback()
            raise ValidationError("Baseline scan analysis is not completed.")
        return baseline

    # ------------------------------------------------------------------
    # Provider validation
    # ------------------------------------------------------------------

    def _validate_baseline_providers(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        baseline: Scan,
    ) -> list[BaselineProviderTarget]:
        baseline_runs = self._runs.list_by_scan(baseline.id)
        seen: dict[LLMProvider, BaselineProviderTarget] = {}
        for run in baseline_runs:
            if run.provider not in seen:
                seen[run.provider] = BaselineProviderTarget(
                    provider=run.provider,
                    surface=run.provider_surface,
                    mode=run.execution_mode,
                    requested_model=run.requested_model,
                )
        targets = [seen[p] for p in PROVIDER_ORDER if p in seen]
        if not targets:
            self._session.rollback()
            raise ValidationError("Baseline scan has no provider targets.")

        entitlements = self._entitlements.get_effective_entitlements(workspace_id)
        configured = {
            row.provider for row in self._project_providers.list_enabled_by_project(project_id)
        }
        for target in targets:
            if target.provider not in entitlements.allowed_providers:
                self._session.rollback()
                raise EntitlementDeniedError(
                    f"Provider '{target.provider}' is no longer allowed "
                    f"by the current plan. Verification Scan requires all "
                    f"baseline providers to remain allowed."
                )
            if target.provider not in configured:
                self._session.rollback()
                raise ConflictError(
                    f"Provider '{target.provider}' is no longer enabled "
                    f"for this project. Verification Scan requires all "
                    f"baseline providers to remain enabled."
                )
            adapter = self._registry.get(target.provider)
            capabilities = adapter.capabilities()
            if (
                target.mode == ProviderExecutionMode.MODEL_ONLY
                and not capabilities.supports_model_only
            ):
                self._session.rollback()
                raise ConflictError(
                    f"Provider '{target.provider}' does not support MODEL_ONLY mode."
                )
            if (
                target.mode == ProviderExecutionMode.WEB_GROUNDED
                and not capabilities.supports_web_grounded
            ):
                self._session.rollback()
                raise ConflictError(
                    f"Provider '{target.provider}' does not support WEB_GROUNDED mode."
                )
        return targets

    # ------------------------------------------------------------------
    # Pricing preflight
    # ------------------------------------------------------------------

    def _pricing_preflight(self, targets: list[BaselineProviderTarget]) -> None:
        if not self._settings.pricing_require_rule_for_execution:
            return
        now = datetime.now(UTC)
        for target in targets:
            if target.provider == LLMProvider.PERPLEXITY:
                continue
            try:
                self._pricing.resolve(
                    target.provider,
                    target.surface,
                    target.requested_model,
                    now,
                )
            except PricingRuleNotFoundError as exc:
                self._session.rollback()
                raise ConflictError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Baseline prompts
    # ------------------------------------------------------------------

    def _load_baseline_prompts(self, baseline: Scan) -> list[Prompt]:
        baseline_runs = self._runs.list_by_scan(baseline.id)
        seen_ids: set[uuid.UUID] = set()
        prompt_ids: list[uuid.UUID] = []
        for run in baseline_runs:
            if run.prompt_id not in seen_ids:
                seen_ids.add(run.prompt_id)
                prompt_ids.append(run.prompt_id)
        prompts: list[Prompt] = []
        for pid in prompt_ids:
            prompt = self._session.get(Prompt, pid)
            if prompt is None:
                self._session.rollback()
                raise ValidationError("Baseline prompt is no longer available.")
            prompts.append(prompt)
        return prompts

    # ------------------------------------------------------------------
    # PromptRun creation (single observation)
    # ------------------------------------------------------------------

    def _create_verification_runs(
        self,
        scan_id: uuid.UUID,
        prompts: list[Prompt],
        targets: list[BaselineProviderTarget],
    ) -> list[PromptRun]:
        runs: list[PromptRun] = []
        for prompt in prompts:
            for target in targets:
                runs.append(
                    PromptRun(
                        scan_id=scan_id,
                        prompt_id=prompt.id,
                        provider=target.provider,
                        provider_surface=target.surface,
                        execution_mode=target.mode,
                        requested_model=target.requested_model,
                        status=PromptRunStatus.PENDING,
                        attempt_number=1,
                        observation_index=1,
                    )
                )
        return runs

    # ------------------------------------------------------------------
    # Entity snapshot cloning
    # ------------------------------------------------------------------

    def _clone_entity_snapshots(
        self,
        scan_id: uuid.UUID,
        baseline_snapshots: list[ScanEntitySnapshot],
    ) -> None:
        clones = [
            ScanEntitySnapshot(
                scan_id=scan_id,
                entity_key=snap.entity_key,
                entity_type=snap.entity_type,
                name=snap.name,
                domain=snap.domain,
                aliases=list(snap.aliases),
                source_competitor_id=snap.source_competitor_id,
                ordinal=snap.ordinal,
            )
            for snap in baseline_snapshots
        ]
        self._snapshots.create_batch(clones)

    # ------------------------------------------------------------------
    # Idempotency / dispatch helpers
    # ------------------------------------------------------------------

    def _find_existing_verification(
        self, workspace_id: uuid.UUID, idempotency_key: str
    ) -> OpportunityVerification | None:
        return self._session.execute(
            select(OpportunityVerification).where(
                OpportunityVerification.workspace_id == workspace_id,
                OpportunityVerification.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()

    def _validate_existing_verification(
        self,
        existing: OpportunityVerification,
        project_id: uuid.UUID,
        opportunity_id: uuid.UUID,
    ) -> None:
        if existing.project_id != project_id:
            raise ConflictError("Idempotency-Key reused for a conflicting verification request.")
        if existing.opportunity_id != opportunity_id:
            raise ConflictError("Idempotency-Key reused for a conflicting verification request.")

    def _resume_dispatch_if_needed(
        self, scan: Scan, verification: OpportunityVerification
    ) -> VerificationScanCreationResult:
        if (
            scan.status == ScanStatus.PENDING
            and scan.quota_reservation_id is not None
            and scan.dispatched_at is None
        ):
            return self._dispatch(scan, verification, created=False)
        return VerificationScanCreationResult(
            scan=scan, verification=verification, created=False, dispatched=False
        )

    def _dispatch(
        self, scan: Scan, verification: OpportunityVerification, *, created: bool
    ) -> VerificationScanCreationResult:
        try:
            self._dispatcher.dispatch(scan.id)
        except Exception as exc:
            scan.failure_code = "DISPATCH_FAILED"
            scan.failure_message = "Scan dispatch is temporarily unavailable."
            self._session.commit()
            logger.error(
                "verification_scan_dispatch_failed",
                scan_id=str(scan.id),
                error_type=type(exc).__name__,
            )
            raise InfrastructureError("Scan dispatch is temporarily unavailable.") from exc
        scan.dispatched_at = datetime.now(UTC)
        scan.failure_code = None
        scan.failure_message = None
        self._session.commit()
        self._record_audit("SCAN_DISPATCHED", scan)
        return VerificationScanCreationResult(
            scan=scan, verification=verification, created=created, dispatched=True
        )

    def _mark_quota_failed(self, scan_id: uuid.UUID, message: str) -> None:
        scan = self._scans.get_for_update(scan_id)
        if scan is None:
            self._session.rollback()
            return
        now = datetime.now(UTC)
        scan.status = ScanStatus.FAILED
        scan.failed_runs = scan.planned_ai_checks
        scan.completed_at = now
        scan.failure_code = "QUOTA_EXCEEDED"
        scan.failure_message = message[:1000]
        self._runs.mark_unresolved_failed(scan.id, now, "Quota reservation failed.")
        self._session.commit()
        self._record_audit("SCAN_FAILED", scan)

    def _record_audit(self, action: str, scan: Scan) -> None:
        if self._audit is not None:
            self._audit.record(
                action=action,
                workspace_id=scan.workspace_id,
                user_id=scan.requested_by_user_id,
                entity_type="scan",
                entity_id=scan.id,
            )
