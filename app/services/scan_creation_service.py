"""STANDARD scan preflight, snapshot planning, quota reservation, and dispatch."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.enums import (
    ProjectStatus,
    PromptRunStatus,
    ProviderExecutionMode,
    ScanStatus,
    ScanType,
)
from app.core.exceptions import (
    ConflictError,
    InfrastructureError,
    NotFoundError,
    PricingRuleNotFoundError,
    QuotaExceededError,
    ValidationError,
)
from app.core.logging import get_logger
from app.models.project import Project
from app.models.scan import PromptRun, Scan
from app.models.tracking import Prompt
from app.providers.registry import ProviderRegistry
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import PromptRunRepository, ScanRepository
from app.repositories.tracking_repository import (
    ProjectProviderRepository,
    PromptRepository,
    PromptSetRepository,
)
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService
from app.services.pricing_service import PricingService
from app.services.prompt_generation_service import GENERATOR_KEY
from app.services.quota_service import QuotaService
from app.services.scanning.dispatcher import ScanDispatcher
from app.services.scanning.policy import (
    PROVIDER_ORDER,
    ProviderExecutionPolicy,
    ProviderExecutionTarget,
)

logger = get_logger("app.scan_creation")


@dataclass(frozen=True)
class ScanCreationResult:
    scan: Scan
    created: bool
    dispatched: bool


class ScanCreationService:
    """Create one immutable STANDARD plan before any provider request."""

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
        self._prompt_sets = PromptSetRepository(session)
        self._prompts = PromptRepository(session)
        self._project_providers = ProjectProviderRepository(session)
        self._scans = ScanRepository(session)
        self._runs = PromptRunRepository(session)
        self._entitlements = EntitlementService(session)
        self._pricing = PricingService(session)
        self._policy = ProviderExecutionPolicy()
        self._quota = QuotaService(session, audit_service=audit_service)

    def create_scan(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scan_type: ScanType,
        requested_by_user_id: uuid.UUID | None,
        idempotency_key: str,
    ) -> ScanCreationResult:
        key = self.normalize_idempotency_key(idempotency_key)
        project = self._projects.get_in_workspace_for_update(project_id, workspace_id)
        if project is None:
            self._session.rollback()
            raise NotFoundError("Project not found.")

        existing = self._scans.get_by_idempotency_key(workspace_id, key)
        if existing is not None:
            self._validate_existing(existing, project_id, scan_type)
            self._session.commit()
            return self._resume_dispatch_if_needed(existing)

        try:
            prompts, targets = self._preflight(project, scan_type)
            scan = Scan(
                workspace_id=workspace_id,
                project_id=project.id,
                prompt_set_id=prompts[0].prompt_set_id,
                scan_type=scan_type,
                status=ScanStatus.PENDING,
                requested_by_user_id=requested_by_user_id,
                idempotency_key=key,
                prompt_count=len(prompts),
                provider_count=len(targets),
                planned_ai_checks=len(prompts) * len(targets),
                successful_runs=0,
                failed_runs=0,
            )
            self._scans.create(scan)
            planned_runs = [
                PromptRun(
                    scan_id=scan.id,
                    prompt_id=prompt.id,
                    provider=target.provider,
                    provider_surface=target.surface,
                    execution_mode=target.mode,
                    requested_model=target.requested_model,
                    status=PromptRunStatus.PENDING,
                    attempt_number=1,
                )
                for prompt in prompts
                for target in targets
            ]
            self._runs.create_batch(planned_runs)
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            existing = self._scans.get_by_idempotency_key(workspace_id, key)
            if existing is None:
                raise
            self._validate_existing(existing, project_id, scan_type)
            return self._resume_dispatch_if_needed(existing)
        except Exception:
            self._session.rollback()
            raise

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

    @staticmethod
    def normalize_idempotency_key(value: str) -> str:
        key = value.strip()
        if not key:
            raise ValidationError("Idempotency-Key must not be empty.")
        if len(key) > 255:
            raise ValidationError("Idempotency-Key must not exceed 255 characters.")
        return key

    def _preflight(
        self, project: Project, scan_type: ScanType
    ) -> tuple[list[Prompt], list[ProviderExecutionTarget]]:
        if scan_type != ScanType.STANDARD:
            raise ValidationError(f"Scan type {scan_type.value} is not supported in Phase 6.")
        if project.status != ProjectStatus.ACTIVE:
            raise ConflictError("Project must be ACTIVE to start a scan.")

        prompt_set = self._prompt_sets.get_active_by_project(project.id)
        if prompt_set is None:
            raise ConflictError("Project has no ACTIVE PromptSet.")
        if prompt_set.input_revision != project.prompt_input_revision:
            raise ConflictError("PromptSet is stale; regenerate prompts before scanning.")
        if prompt_set.generator_key != GENERATOR_KEY:
            raise ConflictError("PromptSet generator is stale; regenerate prompts before scanning.")
        prompts = [
            prompt for prompt in self._prompts.list_by_prompt_set(prompt_set.id) if prompt.active
        ]
        if not prompts:
            raise ConflictError("PromptSet contains no active prompts.")

        entitlements = self._entitlements.get_effective_entitlements(project.workspace_id)
        configured = {
            row.provider for row in self._project_providers.list_enabled_by_project(project.id)
        }
        eligible = [
            provider
            for provider in PROVIDER_ORDER
            if provider in configured and provider in entitlements.allowed_providers
        ]
        if not eligible:
            raise ConflictError("No enabled project provider is allowed by the current plan.")

        now = datetime.now(UTC)
        targets: list[ProviderExecutionTarget] = []
        for provider in eligible:
            target = self._policy.target(provider, self._settings)
            if not target.requested_model:
                raise ConflictError(f"Provider {provider.value} scan model is not configured.")
            adapter = self._registry.get(provider)
            capabilities = adapter.capabilities()
            if (
                target.mode == ProviderExecutionMode.MODEL_ONLY
                and not capabilities.supports_model_only
            ):
                raise ConflictError(f"Provider {provider.value} does not support MODEL_ONLY.")
            if (
                target.mode == ProviderExecutionMode.WEB_GROUNDED
                and not capabilities.supports_web_grounded
            ):
                raise ConflictError(f"Provider {provider.value} does not support WEB_GROUNDED.")
            if self._settings.pricing_require_rule_for_execution and provider.value != "PERPLEXITY":
                try:
                    self._pricing.resolve(provider, target.surface, target.requested_model, now)
                except PricingRuleNotFoundError as exc:
                    raise ConflictError(str(exc)) from exc
            targets.append(target)
        return prompts, targets

    @staticmethod
    def _validate_existing(existing: Scan, project_id: uuid.UUID, scan_type: ScanType) -> None:
        if existing.project_id != project_id or existing.scan_type != scan_type:
            raise ConflictError("Idempotency-Key reused for a conflicting scan request.")
        if existing.failure_code == "QUOTA_EXCEEDED":
            raise QuotaExceededError(existing.failure_message or "AI Check quota exceeded.")

    def _resume_dispatch_if_needed(self, scan: Scan) -> ScanCreationResult:
        if (
            scan.status == ScanStatus.PENDING
            and scan.quota_reservation_id is not None
            and scan.dispatched_at is None
        ):
            return self._dispatch(scan, created=False)
        return ScanCreationResult(scan=scan, created=False, dispatched=False)

    def _dispatch(self, scan: Scan, *, created: bool) -> ScanCreationResult:
        try:
            self._dispatcher.dispatch(scan.id)
        except Exception as exc:
            scan.failure_code = "DISPATCH_FAILED"
            scan.failure_message = "Scan dispatch is temporarily unavailable."
            self._session.commit()
            logger.error(
                "scan_dispatch_failed", scan_id=str(scan.id), error_type=type(exc).__name__
            )
            raise InfrastructureError("Scan dispatch is temporarily unavailable.") from exc
        scan.dispatched_at = datetime.now(UTC)
        scan.failure_code = None
        scan.failure_message = None
        self._session.commit()
        self._record_audit("SCAN_DISPATCHED", scan)
        return ScanCreationResult(scan=scan, created=created, dispatched=True)

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
