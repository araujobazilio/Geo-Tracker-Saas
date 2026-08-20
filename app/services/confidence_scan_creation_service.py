"""Confidence Scan creation -- clone a baseline STANDARD scan's methodology.

A Confidence Scan repeats the SAME immutable measurement cells (Prompt x
Provider) multiple times so GEO Tracker can show how stable or variable
the observed visibility is.

Key invariants:
- Baseline must be a COMPLETED or PARTIAL STANDARD scan with successful
  runs, zero unresolved PromptRuns, entity snapshots, and a full
  immutable PromptRun plan.
- The baseline's exact prompt_set_id, prompt IDs, provider surfaces,
  execution modes, requested models, and entity snapshots are cloned.
- Current project configuration (brand, competitors, keywords, providers,
  PromptSet) must NOT alter the cloned methodology.
- Current entitlements and provider configuration must still allow ALL
  baseline providers. If any baseline provider is no longer
  allowed/configured, the entire Confidence Scan is rejected.
- repeat_count is validated against CONFIDENCE_SCAN_DEFAULT_REPEATS and
  CONFIDENCE_SCAN_MAX_REPEATS.
- Quota is reserved for prompt_count x provider_count x repeat_count
  AI Checks before dispatch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.enums import (
    LLMProvider,
    ProjectStatus,
    PromptRunStatus,
    ProviderExecutionMode,
    ProviderSurface,
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
from app.models.analysis import ScanEntitySnapshot
from app.models.scan import PromptRun, Scan
from app.models.tracking import Prompt
from app.providers.registry import ProviderRegistry
from app.repositories.analysis_repository import ScanEntitySnapshotRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import PromptRunRepository, ScanRepository
from app.repositories.tracking_repository import (
    ProjectProviderRepository,
    PromptRepository,
)
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService
from app.services.pricing_service import PricingService
from app.services.quota_service import QuotaService
from app.services.scanning.dispatcher import ScanDispatcher
from app.services.scanning.policy import PROVIDER_ORDER

logger = get_logger("app.confidence_scan_creation")


@dataclass(frozen=True)
class ConfidenceScanCreationResult:
    scan: Scan
    created: bool
    dispatched: bool


# Baseline scan statuses eligible for Confidence creation.
_BASELINE_ELIGIBLE_STATUSES = (ScanStatus.COMPLETED, ScanStatus.PARTIAL)


class ConfidenceScanCreationService:
    """Create a CONFIDENCE scan from an existing STANDARD baseline.

    This service is separate from ScanCreationService to keep the
    STANDARD creation path clean and avoid a giant switch statement.
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
        self._prompts = PromptRepository(session)
        self._project_providers = ProjectProviderRepository(session)
        self._entitlements = EntitlementService(session)
        self._pricing = PricingService(session)
        self._quota = QuotaService(session, audit_service=audit_service)

    def create_confidence_scan(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        baseline_scan_id: uuid.UUID,
        requested_by_user_id: uuid.UUID | None,
        idempotency_key: str,
        repeat_count: int | None = None,
    ) -> ConfidenceScanCreationResult:
        key = ScanCreationServiceIdempotencyHelper.normalize(idempotency_key)
        resolved_repeats = self._resolve_repeat_count(repeat_count)

        # Check idempotency: existing scan with same key.
        existing = self._scans.get_by_idempotency_key(workspace_id, key)
        if existing is not None:
            self._validate_existing_confidence(
                existing, project_id, baseline_scan_id, resolved_repeats
            )
            self._session.commit()
            return self._resume_dispatch_if_needed(existing)

        # Load and validate the project (must still be usable).
        project = self._projects.get_in_workspace_for_update(project_id, workspace_id)
        if project is None:
            self._session.rollback()
            raise NotFoundError("Project not found.")
        if project.status != ProjectStatus.ACTIVE:
            self._session.rollback()
            raise ConflictError("Project must be ACTIVE to start a scan.")

        # Load and validate the baseline scan.
        baseline = self._load_and_validate_baseline(workspace_id, project_id, baseline_scan_id)

        # Entitlement: confidence_scans feature must be enabled.
        try:
            self._entitlements.require_feature(workspace_id, "confidence_scans")
        except EntitlementDeniedError:
            self._session.rollback()
            raise

        # Validate all baseline providers are still allowed + configured.
        baseline_targets = self._validate_baseline_providers(workspace_id, project_id, baseline)

        # Pricing preflight for each baseline target.
        self._pricing_preflight(baseline_targets)

        # Load baseline prompts (immutable plan).
        baseline_prompts = self._load_baseline_prompts(baseline)

        # Load baseline entity snapshots.
        baseline_snapshots = self._snapshots.list_by_scan(baseline.id)
        if not baseline_snapshots:
            self._session.rollback()
            raise ConflictError("Baseline scan has no entity snapshots.")

        # Compute planned AI checks.
        prompt_count = len(baseline_prompts)
        provider_count = len(baseline_targets)
        planned_ai_checks = prompt_count * provider_count * resolved_repeats

        try:
            scan = Scan(
                workspace_id=workspace_id,
                project_id=project.id,
                prompt_set_id=baseline.prompt_set_id,
                scan_type=ScanType.CONFIDENCE,
                status=ScanStatus.PENDING,
                requested_by_user_id=requested_by_user_id,
                idempotency_key=key,
                prompt_count=prompt_count,
                provider_count=provider_count,
                planned_ai_checks=planned_ai_checks,
                successful_runs=0,
                failed_runs=0,
                repeat_count=resolved_repeats,
                baseline_scan_id=baseline.id,
            )
            self._scans.create(scan)

            # Create repeated PromptRuns in deterministic order:
            # observation_index → prompt canonical order → provider order.
            planned_runs = self._create_repeated_runs(
                scan.id,
                baseline_prompts,
                baseline_targets,
                resolved_repeats,
            )
            self._runs.create_batch(planned_runs)

            # Clone entity snapshots from baseline.
            self._clone_entity_snapshots(scan.id, baseline_snapshots)

            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self._scans.get_by_idempotency_key(workspace_id, key)
            if existing is None:
                raise
            self._validate_existing_confidence(
                existing, project_id, baseline_scan_id, resolved_repeats
            )
            return self._resume_dispatch_if_needed(existing)
        except Exception:
            self._session.rollback()
            raise

        # Reserve quota for all planned AI Checks.
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
        return self._dispatch(attached_scan, created=True)

    # ------------------------------------------------------------------
    # Repeat-count validation
    # ------------------------------------------------------------------

    def _resolve_repeat_count(self, repeat_count: int | None) -> int:
        default = self._settings.confidence_scan_default_repeats
        maximum = self._settings.confidence_scan_max_repeats
        if repeat_count is None:
            return default
        if repeat_count < 2:
            raise ValidationError("repeat_count must be >= 2 for a Confidence Scan.")
        if repeat_count > maximum:
            raise ValidationError(
                f"repeat_count must be <= {maximum} (CONFIDENCE_SCAN_MAX_REPEATS)."
            )
        return repeat_count

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
        # Check zero unresolved PromptRuns.
        succeeded, failed, unresolved = self._runs.terminal_counts(baseline.id)
        if unresolved > 0:
            self._session.rollback()
            raise ValidationError("Baseline scan has unresolved PromptRuns.")
        # Check entity snapshots exist.
        snapshot_count = self._snapshots.count_by_scan(baseline.id)
        if snapshot_count == 0:
            self._session.rollback()
            raise ValidationError("Baseline scan has no entity snapshots.")
        # Check full immutable PromptRun plan exists.
        run_count = self._runs.count_by_scan(baseline.id)
        if run_count != baseline.planned_ai_checks:
            self._session.rollback()
            raise ValidationError("Baseline scan PromptRun plan is incomplete.")
        return baseline

    # ------------------------------------------------------------------
    # Provider validation — all baseline providers must still be allowed
    # ------------------------------------------------------------------

    def _validate_baseline_providers(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        baseline: Scan,
    ) -> list[BaselineProviderTarget]:
        """Validate that ALL baseline providers are still allowed by
        current entitlements and enabled for the project.

        Returns the unique baseline provider targets in canonical order.
        Does NOT re-run ProviderExecutionPolicy.target() — the baseline's
        exact surface, mode, and requested_model are copied.
        """
        baseline_runs = self._runs.list_by_scan(baseline.id)

        # Extract unique (provider, surface, mode, requested_model) cells.
        seen: dict[LLMProvider, BaselineProviderTarget] = {}
        for run in baseline_runs:
            if run.provider not in seen:
                seen[run.provider] = BaselineProviderTarget(
                    provider=run.provider,
                    surface=run.provider_surface,
                    mode=run.execution_mode,
                    requested_model=run.requested_model,
                )

        # Order by canonical PROVIDER_ORDER.
        targets = [seen[p] for p in PROVIDER_ORDER if p in seen]
        if not targets:
            self._session.rollback()
            raise ValidationError("Baseline scan has no provider targets.")

        # Current entitlements.
        entitlements = self._entitlements.get_effective_entitlements(workspace_id)

        # Current project provider configuration.
        configured = {
            row.provider for row in self._project_providers.list_enabled_by_project(project_id)
        }

        # Validate each baseline provider.
        for target in targets:
            if target.provider not in entitlements.allowed_providers:
                self._session.rollback()
                raise EntitlementDeniedError(
                    f"Provider '{target.provider.value}' is no longer allowed "
                    f"by the current plan. Confidence Scan requires all "
                    f"baseline providers to remain allowed."
                )
            if target.provider not in configured:
                self._session.rollback()
                raise ConflictError(
                    f"Provider '{target.provider.value}' is no longer enabled "
                    f"for this project. Confidence Scan requires all "
                    f"baseline providers to remain enabled."
                )
            # Validate server configuration can execute the baseline model.
            adapter = self._registry.get(target.provider)
            capabilities = adapter.capabilities()
            if (
                target.mode == ProviderExecutionMode.MODEL_ONLY
                and not capabilities.supports_model_only
            ):
                self._session.rollback()
                raise ConflictError(
                    f"Provider '{target.provider.value}' does not support " f"MODEL_ONLY mode."
                )
            if (
                target.mode == ProviderExecutionMode.WEB_GROUNDED
                and not capabilities.supports_web_grounded
            ):
                self._session.rollback()
                raise ConflictError(
                    f"Provider '{target.provider.value}' does not support " f"WEB_GROUNDED mode."
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
            if target.provider.value == "PERPLEXITY":
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
        """Load the baseline's prompt IDs in canonical order.

        The prompts are loaded from the immutable PromptRun plan, not
        from the current PromptSet, to ensure historical reproducibility.
        """
        baseline_runs = self._runs.list_by_scan(baseline.id)
        # Extract unique prompt IDs in the order they appear (created_at, id).
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
    # Repeated PromptRun creation
    # ------------------------------------------------------------------

    def _create_repeated_runs(
        self,
        scan_id: uuid.UUID,
        prompts: list[Prompt],
        targets: list[BaselineProviderTarget],
        repeat_count: int,
    ) -> list[PromptRun]:
        """Create repeated PromptRuns in deterministic order.

        Order: observation_index → prompt canonical order → provider order.
        """
        runs: list[PromptRun] = []
        for obs_idx in range(1, repeat_count + 1):
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
                            observation_index=obs_idx,
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
        """Clone baseline entity snapshots exactly.

        The copied snapshot values are authoritative. source_competitor_id
        is preserved when safe (the competitor may have been deleted, but
        the FK is SET NULL).
        """
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

    def _validate_existing_confidence(
        self,
        existing: Scan,
        project_id: uuid.UUID,
        baseline_scan_id: uuid.UUID,
        repeat_count: int,
    ) -> None:
        if existing.project_id != project_id:
            raise ConflictError("Idempotency-Key reused for a conflicting scan request.")
        if existing.scan_type != ScanType.CONFIDENCE:
            raise ConflictError("Idempotency-Key reused for a conflicting scan request.")
        if existing.baseline_scan_id != baseline_scan_id:
            raise ConflictError("Idempotency-Key reused with a different baseline_scan_id.")
        if existing.repeat_count != repeat_count:
            raise ConflictError("Idempotency-Key reused with a different repeat_count.")
        if existing.failure_code == "QUOTA_EXCEEDED":
            raise QuotaExceededError(existing.failure_message or "AI Check quota exceeded.")

    def _resume_dispatch_if_needed(self, scan: Scan) -> ConfidenceScanCreationResult:
        if (
            scan.status == ScanStatus.PENDING
            and scan.quota_reservation_id is not None
            and scan.dispatched_at is None
        ):
            return self._dispatch(scan, created=False)
        return ConfidenceScanCreationResult(scan=scan, created=False, dispatched=False)

    def _dispatch(self, scan: Scan, *, created: bool) -> ConfidenceScanCreationResult:
        try:
            self._dispatcher.dispatch(scan.id)
        except Exception as exc:
            scan.failure_code = "DISPATCH_FAILED"
            scan.failure_message = "Scan dispatch is temporarily unavailable."
            self._session.commit()
            logger.error(
                "confidence_scan_dispatch_failed",
                scan_id=str(scan.id),
                error_type=type(exc).__name__,
            )
            raise InfrastructureError("Scan dispatch is temporarily unavailable.") from exc
        scan.dispatched_at = datetime.now(UTC)
        scan.failure_code = None
        scan.failure_message = None
        self._session.commit()
        self._record_audit("SCAN_DISPATCHED", scan)
        return ConfidenceScanCreationResult(scan=scan, created=created, dispatched=True)

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


# ----------------------------------------------------------------------
# Helper dataclasses
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class BaselineProviderTarget:
    """A provider target cloned from the baseline scan.

    Unlike ProviderExecutionTarget, this is NOT derived from current
    settings — it snapshots the baseline's exact surface, mode, and
    requested_model.
    """

    provider: LLMProvider
    surface: ProviderSurface
    mode: ProviderExecutionMode
    requested_model: str


class ScanCreationServiceIdempotencyHelper:
    """Shared idempotency-key normalization logic."""

    @staticmethod
    def normalize(value: str) -> str:
        key = value.strip()
        if not key:
            raise ValidationError("Idempotency-Key must not be empty.")
        if len(key) > 255:
            raise ValidationError("Idempotency-Key must not exceed 255 characters.")
        return key
