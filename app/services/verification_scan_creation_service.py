"""Verification Scan creation — clone a baseline STANDARD scan's methodology
for Opportunity verification (Phase 10 + Phase 10.1).

A Verification Scan repeats the SAME immutable measurement cells (Prompt x
Provider) as the frozen implementation baseline STANDARD scan, exactly
once (repeat_count=1). The resulting evidence is compared deterministically
against the baseline by VerificationEvaluationService.

Phase 10.1 hardening:
- Targeted scope: only the exact historical baseline cells relevant to
  the Opportunity type are re-measured (not a full Cartesian product).
- planned_ai_checks = len(target_cells), NOT prompt_count * provider_count.
- Only providers in the selected scope are validated (not all baseline
  providers).
- Pricing preflight runs only against the exact selected cells.
- One active (PENDING) verification per implementation cycle is enforced
  at the service level and via a partial unique index.
- Idempotency key reuse across different implementation baselines raises
  ConflictError.
- The same scope drives BOTH execution and evaluation.

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
- The verification_scans entitlement must be enabled.
- Quota is reserved for planned_ai_checks (= len(target_cells)) before
  dispatch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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
    ProviderSurface,
    ScanAnalysisStatus,
    ScanStatus,
    ScanType,
    VerificationOutcome,
    VerificationReasonCode,
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
from app.core.verification_scope import VerificationScope, VerificationScopeResolver
from app.models.analysis import ScanEntitySnapshot
from app.models.opportunity import (
    Opportunity,
    OpportunityOccurrence,
    OpportunityVerification,
)
from app.models.scan import PromptRun, Scan
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
    ScanCreationServiceIdempotencyHelper,
)
from app.services.entitlement_service import EntitlementService
from app.services.pricing_service import PricingService
from app.services.quota_service import QuotaService
from app.services.scanning.dispatcher import ScanDispatcher

logger = get_logger("app.verification_scan_creation")


@dataclass(frozen=True)
class VerificationScanCreationResult:
    scan: Scan
    verification: OpportunityVerification
    created: bool
    dispatched: bool


_BASELINE_ELIGIBLE_STATUSES = (ScanStatus.COMPLETED, ScanStatus.PARTIAL)


class VerificationScanCreationService:
    """Create a VERIFICATION scan from an Opportunity's frozen baseline.

    Phase 10.1: uses VerificationScopeResolver to select the exact
    historical baseline cells relevant to the Opportunity type, rather
    than cloning the full Cartesian product.
    """

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
        self._scope_resolver = VerificationScopeResolver(session)

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

        # Phase 10.1: Check for an existing PENDING verification for the
        # same implementation cycle (opportunity + baseline_occurrence).
        self._check_no_pending_verification(opp.id, baseline_occurrence.id)

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

        # Phase 10.1: Resolve the exact targeted scope.
        scope = self._scope_resolver.resolve(opp, baseline)

        # Phase 10.1: Validate only the providers in the selected scope.
        self._validate_scope_providers(workspace_id, project_id, scope)

        # Phase 10.1: Pricing preflight only against exact selected cells.
        self._pricing_preflight_scope(scope)

        # Load baseline entity snapshots.
        baseline_snapshots = self._snapshots.list_by_scan(baseline.id)
        if not baseline_snapshots:
            self._session.rollback()
            raise ConflictError("Baseline scan has no entity snapshots.")

        # Phase 10.1: planned_ai_checks = len(target_cells).
        prompt_count = scope.prompt_count
        provider_count = scope.provider_count
        planned_ai_checks = scope.planned_ai_checks

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

            planned_runs = self._create_verification_runs(scan.id, scope)
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
        # Save scan + verification data before quota reservation — the
        # quota service calls session.rollback() on QuotaExceededError,
        # which may undo the scan + verification INSERTs when running
        # inside an outer transaction (e.g., integration tests).  If
        # the scan is not found after the rollback, we re-create it as
        # FAILED and the verification as INCONCLUSIVE.
        scan_id_for_quota = scan.id
        verification_id_for_quota = verification.id
        planned_checks = scan.planned_ai_checks
        scan_data = {
            "workspace_id": scan.workspace_id,
            "project_id": scan.project_id,
            "prompt_set_id": scan.prompt_set_id,
            "scan_type": scan.scan_type,
            "requested_by_user_id": scan.requested_by_user_id,
            "idempotency_key": scan.idempotency_key,
            "prompt_count": scan.prompt_count,
            "provider_count": scan.provider_count,
            "planned_ai_checks": scan.planned_ai_checks,
            "repeat_count": scan.repeat_count,
            "baseline_scan_id": scan.baseline_scan_id,
        }
        verification_data = {
            "workspace_id": verification.workspace_id,
            "project_id": verification.project_id,
            "opportunity_id": verification.opportunity_id,
            "baseline_occurrence_id": verification.baseline_occurrence_id,
            "baseline_scan_id": verification.baseline_scan_id,
            "idempotency_key": verification.idempotency_key,
            "verification_methodology_version": verification.verification_methodology_version,
            "metric_name": verification.metric_name,
        }

        try:
            reservation = self._quota.reserve_ai_checks(
                workspace_id=workspace_id,
                requested_checks=planned_checks,
                idempotency_key=f"scan:{scan_id_for_quota}",
                user_id=requested_by_user_id,
                project_id=project_id,
                ttl_seconds=self._settings.scan_reservation_ttl_seconds,
            )
        except QuotaExceededError as exc:
            self._mark_quota_failed(
                scan_id_for_quota,
                verification_id_for_quota,
                str(exc),
                scan_data=scan_data,
                verification_data=verification_data,
            )
            raise

        attached_scan = self._scans.get_by_id(scan_id_for_quota)
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
        """Derive the metric name for the verification comparison.

        Phase 10.1: PROMPT_COMPETITOR_GAP now uses competitor_only_rate
        (percentage points) instead of competitor_only_count (integer).
        """
        opp_type_str = (
            opp.opportunity_type.value
            if hasattr(opp.opportunity_type, "value")
            else str(opp.opportunity_type)
        )
        return {
            "DISCOVERY_VISIBILITY_GAP": "visibility_gap_pp",
            "PROVIDER_VISIBILITY_GAP": "visibility_gap_pp",
            "OWNED_CITATION_GAP": "citation_gap_pp",
            "PROMPT_COMPETITOR_GAP": "competitor_only_rate",
        }.get(opp_type_str, "visibility_gap_pp")

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
    # Phase 10.1: One pending verification per implementation cycle
    # ------------------------------------------------------------------

    def _check_no_pending_verification(
        self, opportunity_id: uuid.UUID, baseline_occurrence_id: uuid.UUID
    ) -> None:
        """Check that no PENDING verification exists for the same
        implementation cycle (opportunity + baseline_occurrence).

        Also checks for verifications whose scan is still PENDING or
        RUNNING, as those represent active provider-spending cycles.

        Phase 10.2: if a PENDING verification exists but its scan is
        definitively FAILED, terminalize it first (releasing the PENDING
        slot) instead of blocking creation.  This prevents a dead scan
        from permanently locking the implementation cycle.
        """
        existing = list(
            self._session.execute(
                select(OpportunityVerification).where(
                    OpportunityVerification.opportunity_id == opportunity_id,
                    OpportunityVerification.baseline_occurrence_id == baseline_occurrence_id,
                )
            ).scalars()
        )
        for ver in existing:
            if ver.outcome != VerificationOutcome.PENDING:
                continue
            # Check if the scan is still active (PENDING or RUNNING).
            scan = self._session.get(Scan, ver.verification_scan_id)
            if scan is not None and scan.status in (ScanStatus.PENDING, ScanStatus.RUNNING):
                self._session.rollback()
                raise ConflictError(
                    "An active verification scan is already in progress "
                    "for this implementation cycle. Wait for it to complete "
                    "or evaluate it before creating a new one."
                )
            # Phase 10.2: if the scan is FAILED, terminalize the
            # verification to release the PENDING slot.
            if scan is not None and scan.status == ScanStatus.FAILED:
                from app.services.verification_lifecycle_service import (
                    VerificationLifecycleService,
                )

                VerificationLifecycleService(self._session).terminalize_failed_scan(ver.id)
                continue
            # If the scan is terminal (COMPLETED/PARTIAL) but evaluation
            # hasn't run, the verification is still PENDING.  Block
            # creation to prevent double spend.
            self._session.rollback()
            raise ConflictError(
                "A pending verification exists for this implementation "
                "cycle. Evaluate it before creating a new one."
            )

    # ------------------------------------------------------------------
    # Phase 10.1: Scoped provider validation
    # ------------------------------------------------------------------

    def _validate_scope_providers(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scope: VerificationScope,
    ) -> None:
        """Validate only the providers in the selected scope.

        An unrelated baseline provider that is currently unavailable
        does NOT block a provider-specific verification.
        """
        entitlements = self._entitlements.get_effective_entitlements(workspace_id)
        configured = {
            row.provider for row in self._project_providers.list_enabled_by_project(project_id)
        }

        for provider in scope.providers:
            if provider not in entitlements.allowed_providers:
                self._session.rollback()
                raise EntitlementDeniedError(
                    f"Provider '{provider}' is no longer allowed "
                    f"by the current plan. Verification Scan requires "
                    f"all in-scope providers to remain allowed."
                )
            if provider not in configured:
                self._session.rollback()
                raise ConflictError(
                    f"Provider '{provider}' is no longer enabled "
                    f"for this project. Verification Scan requires "
                    f"all in-scope providers to remain enabled."
                )

        # Validate adapter capabilities for each unique (provider, mode) in scope.
        seen_caps: set[tuple[LLMProvider, ProviderExecutionMode]] = set()
        for cell in scope.target_cells:
            cap_key = (cell.provider, cell.execution_mode)
            if cap_key in seen_caps:
                continue
            seen_caps.add(cap_key)
            adapter = self._registry.get(cell.provider)
            capabilities = adapter.capabilities()
            if (
                cell.execution_mode == ProviderExecutionMode.MODEL_ONLY
                and not capabilities.supports_model_only
            ):
                self._session.rollback()
                raise ConflictError(f"Provider '{cell.provider}' does not support MODEL_ONLY mode.")
            if (
                cell.execution_mode == ProviderExecutionMode.WEB_GROUNDED
                and not capabilities.supports_web_grounded
            ):
                self._session.rollback()
                raise ConflictError(
                    f"Provider '{cell.provider}' does not support WEB_GROUNDED mode."
                )

    # ------------------------------------------------------------------
    # Phase 10.1: Scoped pricing preflight
    # ------------------------------------------------------------------

    def _pricing_preflight_scope(self, scope: VerificationScope) -> None:
        """Run pricing preflight only against exact selected cells."""
        if not self._settings.pricing_require_rule_for_execution:
            return
        now = datetime.now(UTC)
        seen: set[tuple[LLMProvider, ProviderSurface, str]] = set()
        for cell in scope.target_cells:
            if cell.provider == LLMProvider.PERPLEXITY:
                continue
            price_key = (cell.provider, cell.provider_surface, cell.requested_model)
            if price_key in seen:
                continue
            seen.add(price_key)
            try:
                self._pricing.resolve(
                    cell.provider,
                    cell.provider_surface,
                    cell.requested_model,
                    now,
                )
            except PricingRuleNotFoundError as exc:
                self._session.rollback()
                raise ConflictError(str(exc)) from exc

    # ------------------------------------------------------------------
    # PromptRun creation (single observation, exact target cells)
    # ------------------------------------------------------------------

    def _create_verification_runs(
        self,
        scan_id: uuid.UUID,
        scope: VerificationScope,
    ) -> list[PromptRun]:
        """Create PromptRun rows for each exact target cell in the scope."""
        return [
            PromptRun(
                scan_id=scan_id,
                prompt_id=cell.prompt_id,
                provider=cell.provider,
                provider_surface=cell.provider_surface,
                execution_mode=cell.execution_mode,
                requested_model=cell.requested_model,
                status=PromptRunStatus.PENDING,
                attempt_number=1,
                observation_index=1,
            )
            for cell in scope.target_cells
        ]

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
        # Phase 10.1: if the same key is reused but the implementation
        # baseline has changed (re-implementation cycle), this is a
        # conflict — the caller should use a new idempotency key.
        # We check this by loading the current opportunity's frozen
        # baseline occurrence and comparing it to the existing
        # verification's baseline_occurrence_id.
        opp = self._session.get(Opportunity, opportunity_id)
        if (
            opp is not None
            and opp.implementation_baseline_occurrence_id is not None
            and existing.baseline_occurrence_id != opp.implementation_baseline_occurrence_id
        ):
            raise ConflictError(
                "Idempotency-Key reused but the implementation baseline "
                "has changed (re-implementation cycle). Use a new "
                "idempotency key."
            )

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

    def _mark_quota_failed(
        self,
        scan_id: uuid.UUID,
        verification_id: uuid.UUID,
        message: str,
        *,
        scan_data: dict[str, Any] | None = None,
        verification_data: dict[str, Any] | None = None,
    ) -> None:
        """Mark a scan as FAILED due to quota reservation failure.

        Also terminalizes the associated verification as INCONCLUSIVE
        with reason VERIFICATION_SCAN_FAILED.

        In production, the scan was committed before the quota
        reservation, so it survives the quota service's rollback.
        The scan_data/verification_data parameters are used only if
        the scan was destroyed by the rollback (e.g., in tests with
        transaction-wrapped sessions).
        """
        now = datetime.now(UTC)

        # Try to load the scan — it may have been destroyed by the
        # quota service's session.rollback().
        self._session.expire_all()
        scan = self._session.get(Scan, scan_id)
        if scan is not None:
            # Scan still exists — mark it as FAILED.
            scan.status = ScanStatus.FAILED
            scan.failed_runs = scan.planned_ai_checks
            scan.completed_at = now
            scan.failure_code = "QUOTA_EXCEEDED"
            scan.failure_message = message[:1000]
            self._runs.mark_unresolved_failed(scan.id, now, "Quota reservation failed.")
            self._session.commit()
            self._record_audit("SCAN_FAILED", scan)
        elif scan_data is not None:
            # Scan was destroyed by the rollback — re-create it as FAILED.
            logger.warning(
                "quota_failed_scan_recreated",
                scan_id=str(scan_id),
            )
            failed_scan = Scan(
                id=scan_id,
                workspace_id=scan_data["workspace_id"],
                project_id=scan_data["project_id"],
                prompt_set_id=scan_data["prompt_set_id"],
                scan_type=scan_data["scan_type"],
                status=ScanStatus.FAILED,
                requested_by_user_id=scan_data["requested_by_user_id"],
                idempotency_key=scan_data["idempotency_key"],
                prompt_count=scan_data["prompt_count"],
                provider_count=scan_data["provider_count"],
                planned_ai_checks=scan_data["planned_ai_checks"],
                successful_runs=0,
                failed_runs=scan_data["planned_ai_checks"],
                repeat_count=scan_data["repeat_count"],
                baseline_scan_id=scan_data["baseline_scan_id"],
                completed_at=now,
                failure_code="QUOTA_EXCEEDED",
                failure_message=message[:1000],
            )
            self._scans.create(failed_scan)
            self._session.commit()

        # Terminalize the verification as INCONCLUSIVE.
        ver = self._session.get(OpportunityVerification, verification_id)
        if ver is not None:
            ver.outcome = VerificationOutcome.INCONCLUSIVE
            ver.reason_code = VerificationReasonCode.VERIFICATION_SCAN_FAILED
            ver.evaluation_message = (
                "The verification measurement could not start because "
                "required quota could not be reserved."
            )
            ver.evaluated_at = now
            self._session.commit()
        elif verification_data is not None:
            # Verification was destroyed by the rollback — re-create
            # it as INCONCLUSIVE.
            failed_ver = OpportunityVerification(
                id=verification_id,
                workspace_id=verification_data["workspace_id"],
                project_id=verification_data["project_id"],
                opportunity_id=verification_data["opportunity_id"],
                baseline_occurrence_id=verification_data["baseline_occurrence_id"],
                baseline_scan_id=verification_data["baseline_scan_id"],
                verification_scan_id=scan_id,
                idempotency_key=verification_data["idempotency_key"],
                verification_methodology_version=verification_data[
                    "verification_methodology_version"
                ],
                metric_name=verification_data["metric_name"],
                outcome=VerificationOutcome.INCONCLUSIVE,
                reason_code=VerificationReasonCode.VERIFICATION_SCAN_FAILED,
                evaluation_message=(
                    "The verification measurement could not start because "
                    "required quota could not be reserved."
                ),
                evaluated_at=now,
            )
            self._session.add(failed_ver)
            self._session.commit()

    def _record_audit(self, action: str, scan: Scan) -> None:
        if self._audit is not None:
            self._audit.record(
                action=action,
                workspace_id=scan.workspace_id,
                user_id=scan.requested_by_user_id,
                entity_type="scan",
                entity_id=scan.id,
            )

    def _terminalize_verification_for_failed_scan(self, scan_id: uuid.UUID, message: str) -> None:
        """Phase 10.2: terminalize a PENDING verification whose scan FAILED.

        This releases the partial unique PENDING slot so a new
        verification can be created for the same implementation cycle.
        Zero AI Checks, zero provider calls.
        """
        from app.services.verification_lifecycle_service import (
            VerificationLifecycleService,
        )

        verification = self._session.execute(
            select(OpportunityVerification).where(
                OpportunityVerification.verification_scan_id == scan_id
            )
        ).scalar_one_or_none()
        if verification is None:
            return
        VerificationLifecycleService(self._session).terminalize_failed_scan(
            verification.id,
            reason_code=VerificationReasonCode.VERIFICATION_SCAN_FAILED,
            message=message,
        )
