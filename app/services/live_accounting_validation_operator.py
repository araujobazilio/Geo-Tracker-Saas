"""Canonical operator for one isolated live provider validation.

This module is intentionally not wired to FastAPI or Celery.  It creates a
minimal, private scan plan and then delegates execution to the same
``ScanExecutionService`` used by the product worker.  The operator itself
never calls a provider, calculates pricing, or dispatches a task.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.core.enums import (
    LLMProvider,
    ProjectStatus,
    PromptRunStatus,
    PromptSetStatus,
    PromptType,
    ProviderErrorCode,
    ProviderExecutionMode,
    ProviderSurface,
    ScanStatus,
    ScanType,
    TrackedEntityType,
)
from app.core.exceptions import AppError
from app.core.normalization import normalize_domain, normalize_keyword
from app.models.analysis import ScanEntitySnapshot
from app.models.project import Project
from app.models.prompt_set import PromptSet
from app.models.scan import PromptRun, Scan
from app.models.tracking import ProjectKeyword, ProjectProvider, Prompt
from app.providers.evidence import (
    EVIDENCE_BUNDLE_NAMES,
    EvidenceCollisionError,
    FilesystemProviderEvidenceSink,
    prepare_validation_directory,
    validate_evidence_bundle,
    validation_evidence_directory,
)
from app.providers.openai_adapter import OpenAIProviderAdapter
from app.providers.registry import ProviderRegistry
from app.repositories.analysis_repository import ScanEntitySnapshotRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import PromptRunRepository, ScanRepository
from app.repositories.tracking_repository import (
    PromptRepository,
    PromptSetRepository,
)
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.entitlement_service import EntitlementService
from app.services.pricing_service import PricingService
from app.services.quota_service import QuotaService
from app.services.scan_execution_service import ScanExecutionService
from app.services.scanning.policy import ProviderExecutionPolicy, ProviderExecutionTarget

LIVE_VALIDATION_GENERATOR_KEY = "operator-live-validation-v1"
LIVE_VALIDATION_IDEMPOTENCY_PREFIX = "operator-live-validation-v1"
LIVE_PROVIDER_CALL_ACK = "I UNDERSTAND THIS WILL MAKE ONE PAID OPENAI CALL"

WHITEBOARDMAKER_WORKSPACE_ID = uuid.UUID("cd5852d6-729a-47c6-847d-c136a6dbcec7")
WHITEBOARDMAKER_PROJECT_ID = uuid.UUID("41fdf4c2-1c97-4307-9175-f509af9390bc")
WHITEBOARDMAKER_PROMPT_SET_ID = uuid.UUID("86c1d0ea-28a8-44f1-9950-a7e5f0721631")

_VALIDATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class LiveValidationError(AppError):
    """Safe operator error suitable for CLI output."""

    code = "live_validation_blocked"


@dataclass(frozen=True)
class PromptFile:
    """Exact prompt bytes and their decoded/hash representations."""

    path: Path
    raw_bytes: bytes
    text: str
    sha256: str


@dataclass(frozen=True)
class LiveValidationRequest:
    """All operator inputs are explicit; no workspace is inferred."""

    workspace_id: uuid.UUID
    validation_id: str
    prompt_file: Path
    evidence_dir: Path
    provider: LLMProvider = LLMProvider.OPENAI


@dataclass(frozen=True)
class LiveValidationPlan:
    workspace_id: uuid.UUID
    validation_id: str
    prompt_sha256: str
    prompt_bytes: int
    provider: LLMProvider
    surface: ProviderSurface
    execution_mode: ProviderExecutionMode
    requested_model: str
    max_tool_calls: int
    pricing_rule_id: uuid.UUID
    evidence_dir: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": "PLAN_ONLY",
            "workspace_id": str(self.workspace_id),
            "validation_id": self.validation_id,
            "prompt_sha256": self.prompt_sha256,
            "prompt_bytes": self.prompt_bytes,
            "provider": self.provider.value,
            "surface": self.surface.value,
            "execution_mode": self.execution_mode.value,
            "requested_model": self.requested_model,
            "max_tool_calls": self.max_tool_calls,
            "pricing_rule_id": str(self.pricing_rule_id),
            "evidence_dir": str(self.evidence_dir),
            "provider_calls": 0,
            "celery_dispatches": 0,
        }


@dataclass(frozen=True)
class LiveValidationReport:
    """Sanitized durable summary; only sanitized response evidence is persisted."""

    mode: str
    outcome: str
    validation_id: str
    workspace_id: uuid.UUID
    scan_id: uuid.UUID | None
    prompt_run_id: uuid.UUID | None
    project_id: uuid.UUID | None
    prompt_set_id: uuid.UUID | None
    quota_reservation_id: uuid.UUID | None
    scan_status: ScanStatus | None
    prompt_run_status: PromptRunStatus | None
    usage_event_id: uuid.UUID | None
    provider_request_id: str | None
    provider_response_id: str | None
    web_tool_call_count: int | None
    search_action_count: int | None
    open_page_action_count: int | None
    find_in_page_action_count: int | None
    unknown_web_action_count: int | None
    cost_usd: str | None
    calculated_cost_usd: str | None
    cost_source: str | None
    pricing_rule_id: uuid.UUID | None
    evidence_dir: Path
    evidence_artifacts: tuple[Path, ...]
    evidence_error: str | None
    provider_calls: int
    celery_dispatches: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "outcome": self.outcome,
            "validation_id": self.validation_id,
            "workspace_id": str(self.workspace_id),
            "scan_id": _stringify(self.scan_id),
            "prompt_run_id": _stringify(self.prompt_run_id),
            "project_id": _stringify(self.project_id),
            "prompt_set_id": _stringify(self.prompt_set_id),
            "quota_reservation_id": _stringify(self.quota_reservation_id),
            "scan_status": _value(self.scan_status),
            "prompt_run_status": _value(self.prompt_run_status),
            "usage_event_id": _stringify(self.usage_event_id),
            "provider_request_id": self.provider_request_id,
            "provider_response_id": self.provider_response_id,
            "web_tool_call_count": self.web_tool_call_count,
            "search_action_count": self.search_action_count,
            "open_page_action_count": self.open_page_action_count,
            "find_in_page_action_count": self.find_in_page_action_count,
            "unknown_web_action_count": self.unknown_web_action_count,
            "cost_usd": self.cost_usd,
            "calculated_cost_usd": self.calculated_cost_usd,
            "cost_source": self.cost_source,
            "pricing_rule_id": _stringify(self.pricing_rule_id),
            "evidence_dir": str(self.evidence_dir),
            "evidence_artifacts": [str(path) for path in self.evidence_artifacts],
            "evidence_error": self.evidence_error,
            "provider_calls": self.provider_calls,
            "celery_dispatches": self.celery_dispatches,
        }


class LiveAccountingValidationOperator:
    """Plan and execute exactly one isolated OpenAI validation."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings | None = None,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self._factory = session_factory
        self._settings = settings or get_settings()
        self._registry = registry
        self._policy = ProviderExecutionPolicy()

    @staticmethod
    def load_prompt_file(path: Path) -> PromptFile:
        """Read exact UTF-8 bytes; do not trim or append a newline."""

        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            raise LiveValidationError(
                "Prompt file could not be read.", code="prompt_file_error"
            ) from exc
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LiveValidationError(
                "Prompt file must be valid UTF-8.", code="prompt_encoding_error"
            ) from exc
        if not text.strip():
            raise LiveValidationError("Prompt file must not be empty.", code="prompt_empty")
        if len(text) > 1000:
            raise LiveValidationError(
                "Prompt file exceeds the Prompt column limit.", code="prompt_too_long"
            )
        return PromptFile(
            path=path,
            raw_bytes=raw_bytes,
            text=text,
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
        )

    def plan(self, request: LiveValidationRequest, prompt: PromptFile) -> LiveValidationPlan:
        """Perform read-only preflight; this method never creates DB rows."""

        self._validate_request(request, prompt)
        target = self._target(request.provider)
        pricing_rule_id = self._read_only_preflight(request, target)
        return LiveValidationPlan(
            workspace_id=request.workspace_id,
            validation_id=request.validation_id,
            prompt_sha256=prompt.sha256,
            prompt_bytes=len(prompt.raw_bytes),
            provider=request.provider,
            surface=target.surface,
            execution_mode=target.mode,
            requested_model=target.requested_model,
            max_tool_calls=self._settings.openai_web_search_max_tool_calls,
            pricing_rule_id=pricing_rule_id,
            evidence_dir=request.evidence_dir,
        )

    def execute(
        self,
        request: LiveValidationRequest,
        prompt: PromptFile,
        *,
        acknowledgement: str,
    ) -> LiveValidationReport:
        """Execute one durable plan synchronously, with no dispatch/retry."""

        if acknowledgement != LIVE_PROVIDER_CALL_ACK:
            raise LiveValidationError(
                "Live execution requires the exact paid-call acknowledgement."
            )
        self._validate_request(request, prompt)
        target = self._target(request.provider)
        self._validate_live_registry()

        # Idempotency is checked before preparing evidence or touching quota.
        existing = self._find_existing(request, prompt)
        if existing is not None:
            return existing

        pricing_rule_id = self._read_only_preflight(request, target)
        try:
            evidence_dir = prepare_validation_directory(request.evidence_dir, request.validation_id)
            sink = FilesystemProviderEvidenceSink(
                evidence_dir,
                metadata={
                    "validation_id": request.validation_id,
                    "workspace_id": str(request.workspace_id),
                    "prompt_sha256": prompt.sha256,
                    "requested_model": target.requested_model,
                    "max_tool_calls": self._settings.openai_web_search_max_tool_calls,
                    "pricing_rule_id": str(pricing_rule_id),
                },
            )
            # This is intentionally before the first network-capable operation.
            sink.ensure_ready()
        except EvidenceCollisionError as exc:
            raise LiveValidationError(
                "Validation evidence already exists; manual reconciliation required.",
                code="validation_evidence_collision",
            ) from exc

        scan_id = self._create_durable_plan(request, prompt, target, pricing_rule_id)
        execution_context = self._get_execution_context(scan_id)
        sink.set_execution_context(
            scan_id=execution_context["scan_id"],
            prompt_run_id=execution_context["prompt_run_id"],
            metadata=execution_context,
        )
        self._reserve_exactly_one(scan_id, request)

        registry = self._registry_for_live(sink)

        # The only execution entrypoint is the canonical synchronous call
        # into the async ScanExecutionService.  No Celery object is used.
        try:
            asyncio.run(
                ScanExecutionService(
                    self._factory,
                    registry=registry,
                    settings=self._settings,
                ).execute_scan(scan_id)
            )
        except Exception:
            # A fatal accounting/infrastructure exception must not turn into
            # a retry.  Re-read the durable state so a provider response that
            # already arrived remains visible as RUNNING/ACCOUNTING_UNRESOLVED
            # and can be reconciled manually.
            return self._build_report(request, scan_id, sink)
        return self._build_report(request, scan_id, sink)

    def _validate_request(self, request: LiveValidationRequest, prompt: PromptFile) -> None:
        if request.provider != LLMProvider.OPENAI:
            raise LiveValidationError("Only OPENAI is allowed by the validation operator.")
        if not _VALIDATION_ID.fullmatch(request.validation_id):
            raise LiveValidationError(
                "validation_id must match the documented safe identifier format."
            )
        if request.workspace_id == WHITEBOARDMAKER_WORKSPACE_ID:
            raise LiveValidationError("The protected WhiteboardMaker workspace is denied.")
        if request.evidence_dir == Path(".") or not str(request.evidence_dir):
            raise LiveValidationError("evidence_dir must be explicit.")
        if len(prompt.text) > 1000:
            raise LiveValidationError("Prompt file exceeds the Prompt column limit.")
        if not self._settings.openai_api_key.get_secret_value():
            raise LiveValidationError("OpenAI API key is not configured.")

    def _target(self, provider: LLMProvider) -> ProviderExecutionTarget:
        target = self._policy.target(provider, self._settings)
        if target.surface != ProviderSurface.OPENAI_RESPONSES_API:
            raise LiveValidationError("Unexpected provider surface for live validation.")
        if target.mode != ProviderExecutionMode.WEB_GROUNDED:
            raise LiveValidationError("Live validation requires WEB_GROUNDED mode.")
        if not target.requested_model:
            raise LiveValidationError("OpenAI scan model is not configured.")
        caps = (
            self._registry.capabilities(provider)
            if self._registry is not None
            else ProviderRegistry().capabilities(provider)
        )
        if not caps.supports_web_grounded:
            raise LiveValidationError("OpenAI adapter does not support WEB_GROUNDED mode.")
        return target

    def _validate_live_registry(self) -> None:
        """Reject an injected adapter that cannot carry live evidence."""

        if self._registry is None:
            return
        adapter = self._registry.explicit_adapter(LLMProvider.OPENAI)
        if adapter is not None and not isinstance(adapter, OpenAIProviderAdapter):
            raise LiveValidationError(
                "Live validation requires an instrumentable OpenAI adapter; "
                "custom adapters are offline-test-only."
            )

    def _registry_for_live(self, sink: FilesystemProviderEvidenceSink) -> ProviderRegistry:
        """Clone the registry while instrumenting only its OpenAI adapter."""

        if self._registry is None:
            adapter = OpenAIProviderAdapter(settings=self._settings, evidence_sink=sink)
            return ProviderRegistry({LLMProvider.OPENAI: adapter})

        existing = self._registry.explicit_adapter(LLMProvider.OPENAI)
        if existing is None:
            adapter = OpenAIProviderAdapter(settings=self._settings, evidence_sink=sink)
        elif isinstance(existing, OpenAIProviderAdapter):
            adapter = existing.with_evidence_sink(sink, settings=self._settings)
        else:  # Defensive; _validate_live_registry catches this first.
            raise LiveValidationError("OpenAI adapter cannot be instrumented for live evidence.")
        return self._registry.with_adapter(LLMProvider.OPENAI, adapter)

    def _read_only_preflight(self, request: LiveValidationRequest, target: Any) -> uuid.UUID:
        with self._factory() as session:
            workspace = WorkspaceRepository(session).get_by_id(request.workspace_id)
            if workspace is None:
                raise LiveValidationError("Workspace not found.")
            if workspace.id == WHITEBOARDMAKER_WORKSPACE_ID:
                raise LiveValidationError("The protected WhiteboardMaker workspace is denied.")
            entitlements = EntitlementService(session)
            project_count = ProjectRepository(session).count_tracked_by_workspace(workspace.id)
            try:
                entitlements.require_project_capacity(workspace.id, project_count)
                entitlements.require_provider(workspace.id, LLMProvider.OPENAI)
            except AppError as exc:
                raise LiveValidationError(exc.message, code=exc.code) from exc
            try:
                rule = PricingService(session).resolve(
                    target.provider,
                    target.surface,
                    target.requested_model,
                    datetime.now(UTC),
                )
            except AppError as exc:
                raise LiveValidationError(exc.message, code=exc.code) from exc
            session.rollback()
            return rule.id

    def _find_existing(
        self, request: LiveValidationRequest, prompt: PromptFile
    ) -> LiveValidationReport | None:
        key = self._scan_key(request.validation_id)
        with self._factory() as session:
            scan = ScanRepository(session).get_by_idempotency_key(request.workspace_id, key)
            if scan is None:
                return None
            project = ProjectRepository(session).get_by_id(scan.project_id)
            if project is not None and project.workspace_id != request.workspace_id:
                raise LiveValidationError("Validation project crosses workspace boundaries.")
            prompt_set = PromptSetRepository(session).get_by_id(scan.prompt_set_id)
            runs = PromptRunRepository(session).list_by_scan(scan.id)
            prompts = (
                PromptRepository(session).list_by_prompt_set(prompt_set.id)
                if prompt_set is not None
                else []
            )
            self._validate_durable_shape(scan, project, prompt_set, prompts, runs, prompt)
            if scan.status == ScanStatus.RUNNING or any(
                run.status == PromptRunStatus.RUNNING for run in runs
            ):
                raise LiveValidationError(
                    "Validation already has an ambiguous RUNNING state; manual reconciliation required.",
                    code="validation_running_ambiguous",
                )
            evidence_dir = validation_evidence_directory(
                request.evidence_dir, request.validation_id
            )
            return self._report_from_rows(
                request,
                scan,
                runs[0] if runs else None,
                evidence_error=_existing_evidence_error(
                    evidence_dir,
                    request=request,
                    prompt=prompt,
                    scan=scan,
                    run=runs[0] if runs else None,
                ),
                sink=None,
                evidence_dir=evidence_dir,
                mode="IDEMPOTENT_REUSE",
            )

    def _create_durable_plan(
        self,
        request: LiveValidationRequest,
        prompt_file: PromptFile,
        target: Any,
        pricing_rule_id: uuid.UUID,
    ) -> uuid.UUID:
        key = self._scan_key(request.validation_id)
        with self._factory() as session:
            workspace_repo = WorkspaceRepository(session)
            project_repo = ProjectRepository(session)
            workspace = workspace_repo.get_for_update(request.workspace_id)
            if workspace is None:
                raise LiveValidationError("Workspace not found.")
            if workspace.id == WHITEBOARDMAKER_WORKSPACE_ID:
                raise LiveValidationError("The protected WhiteboardMaker workspace is denied.")
            entitlements = EntitlementService(session)
            entitlements.require_project_capacity(
                workspace.id, project_repo.count_tracked_by_workspace(workspace.id)
            )
            entitlements.require_provider(workspace.id, LLMProvider.OPENAI)

            project_slug = _safe_slug(request.validation_id)
            project = project_repo.create(
                Project(
                    workspace_id=workspace.id,
                    name=f"Live provider validation {request.validation_id}",
                    domain=normalize_domain(f"operator-live-validation-{project_slug}.invalid"),
                    brand_name="GEO Tracker validation",
                    brand_aliases=[],
                    industry="Internal validation",
                    target_country="US",
                    target_language="en",
                    target_audience="Internal provider validation only",
                    status=ProjectStatus.ACTIVE,
                    prompt_input_revision=1,
                )
            )
            keyword_text, normalized_keyword = normalize_keyword(
                f"live provider validation {request.validation_id}"
            )
            keyword = ProjectKeyword(
                project_id=project.id,
                text=keyword_text,
                normalized_text=normalized_keyword,
                active=True,
            )
            session.add(keyword)
            session.flush()
            session.add(
                ProjectProvider(project_id=project.id, provider=LLMProvider.OPENAI, enabled=True)
            )
            prompt_set = PromptSet(
                project_id=project.id,
                version=1,
                input_revision=project.prompt_input_revision,
                status=PromptSetStatus.ACTIVE,
                generator_key=LIVE_VALIDATION_GENERATOR_KEY,
                created_by_user_id=None,
                activated_at=datetime.now(UTC),
            )
            PromptSetRepository(session).create(prompt_set)
            prompt = Prompt(
                prompt_set_id=prompt_set.id,
                project_keyword_id=keyword.id,
                variant_index=1,
                text=prompt_file.text,
                prompt_type=PromptType.NON_BRANDED,
                intent="Internal live provider validation",
                funnel_stage=None,
                persona=None,
                target_country="US",
                target_language="en",
                commercial_intent=False,
                active=True,
            )
            PromptRepository(session).create(prompt)
            scan = Scan(
                workspace_id=workspace.id,
                project_id=project.id,
                prompt_set_id=prompt_set.id,
                scan_type=ScanType.STANDARD,
                status=ScanStatus.PENDING,
                requested_by_user_id=None,
                idempotency_key=key,
                prompt_count=1,
                provider_count=1,
                planned_ai_checks=1,
                successful_runs=0,
                failed_runs=0,
                repeat_count=1,
            )
            ScanRepository(session).create(scan)
            run = PromptRun(
                scan_id=scan.id,
                prompt_id=prompt.id,
                provider=LLMProvider.OPENAI,
                provider_surface=target.surface,
                execution_mode=target.mode,
                requested_model=target.requested_model,
                status=PromptRunStatus.PENDING,
                attempt_number=1,
                observation_index=1,
            )
            PromptRunRepository(session).create_batch([run])
            ScanEntitySnapshotRepository(session).create_batch(
                [
                    ScanEntitySnapshot(
                        scan_id=scan.id,
                        entity_key="brand",
                        entity_type=TrackedEntityType.BRAND,
                        name=project.brand_name,
                        domain=project.domain,
                        aliases=[],
                        source_competitor_id=None,
                        ordinal=1,
                    )
                ]
            )
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                existing = ScanRepository(session).get_by_idempotency_key(request.workspace_id, key)
                if existing is not None:
                    existing_prompt_set = PromptSetRepository(session).get_by_id(
                        existing.prompt_set_id
                    )
                    existing_prompts = PromptRepository(session).list_by_prompt_set(
                        existing.prompt_set_id
                    )
                    existing_runs = PromptRunRepository(session).list_by_scan(existing.id)
                    self._validate_durable_shape(
                        existing,
                        ProjectRepository(session).get_by_id(existing.project_id),
                        existing_prompt_set,
                        existing_prompts,
                        existing_runs,
                        prompt_file,
                        require_quota=False,
                    )
                    return existing.id
                raise LiveValidationError(
                    "Could not persist the isolated validation plan."
                ) from exc
            return scan.id

    def _reserve_exactly_one(self, scan_id: uuid.UUID, request: LiveValidationRequest) -> uuid.UUID:
        with self._factory() as session:
            scan = ScanRepository(session).get_by_id(scan_id)
            if scan is None:
                raise LiveValidationError("Validation scan disappeared before reservation.")
            if scan.quota_reservation_id is not None:
                return scan.quota_reservation_id
            try:
                reservation = QuotaService(session).reserve_ai_checks(
                    workspace_id=request.workspace_id,
                    requested_checks=1,
                    idempotency_key=f"{self._scan_key(request.validation_id)}:quota",
                    user_id=None,
                    project_id=scan.project_id,
                    ttl_seconds=self._settings.scan_reservation_ttl_seconds,
                )
            except AppError as exc:
                self._mark_planning_failure(scan_id, exc.message)
                raise LiveValidationError(exc.message, code=exc.code) from exc
            attached = ScanRepository(session).get_for_update(scan_id)
            if attached is None:
                raise LiveValidationError("Validation scan disappeared after reservation.")
            attached.quota_reservation_id = reservation.id
            session.commit()
            return reservation.id

    def _mark_planning_failure(self, scan_id: uuid.UUID, message: str) -> None:
        with self._factory() as session:
            scan = ScanRepository(session).get_for_update(scan_id)
            if scan is None:
                session.rollback()
                return
            now = datetime.now(UTC)
            scan.status = ScanStatus.FAILED
            scan.failed_runs = scan.planned_ai_checks
            scan.completed_at = now
            scan.failure_code = "QUOTA_EXCEEDED"
            scan.failure_message = message[:1000]
            PromptRunRepository(session).mark_unresolved_failed(
                scan.id, now, "Quota reservation failed."
            )
            session.commit()

    def _build_report(
        self,
        request: LiveValidationRequest,
        scan_id: uuid.UUID,
        sink: FilesystemProviderEvidenceSink,
    ) -> LiveValidationReport:
        with self._factory() as session:
            scan = ScanRepository(session).get_by_id(scan_id)
            runs = PromptRunRepository(session).list_by_scan(scan_id)
            if scan is None or len(runs) != 1:
                raise LiveValidationError("Validation durable state is incomplete.")
            self._validate_durable_shape(
                scan,
                ProjectRepository(session).get_by_id(scan.project_id),
                PromptSetRepository(session).get_by_id(scan.prompt_set_id),
                PromptRepository(session).list_by_prompt_set(scan.prompt_set_id),
                runs,
                None,
            )
            return self._report_from_rows(
                request,
                scan,
                runs[0],
                evidence_error=sink.last_error,
                sink=sink,
                mode="EXECUTE_LIVE",
            )

    def _get_execution_context(self, scan_id: uuid.UUID) -> dict[str, str]:
        with self._factory() as session:
            scan = ScanRepository(session).get_by_id(scan_id)
            runs = PromptRunRepository(session).list_by_scan(scan_id)
            if scan is None or len(runs) != 1:
                raise LiveValidationError("Validation plan does not have exactly one PromptRun.")
            run = runs[0]
            context = {
                "scan_id": str(scan.id),
                "prompt_run_id": str(run.id),
                "project_id": str(scan.project_id),
                "prompt_set_id": str(scan.prompt_set_id),
                "prompt_id": str(run.prompt_id),
                "provider": _value(run.provider) or "",
                "surface": _value(run.provider_surface) or "",
                "requested_model": run.requested_model,
            }
            return context

    def _report_from_rows(
        self,
        request: LiveValidationRequest,
        scan: Scan,
        run: PromptRun | None,
        *,
        evidence_error: str | None,
        sink: FilesystemProviderEvidenceSink | None,
        evidence_dir: Path | None = None,
        mode: str,
    ) -> LiveValidationReport:
        outcome = _classify_outcome(run, evidence_error)
        resolved_evidence_dir = (
            sink.directory if sink is not None else evidence_dir or request.evidence_dir
        )
        evidence_artifacts = (
            tuple(resolved_evidence_dir / name for name in EVIDENCE_BUNDLE_NAMES)
            if sink is not None or evidence_dir is not None
            else ()
        )
        return LiveValidationReport(
            mode=mode,
            outcome=outcome,
            validation_id=request.validation_id,
            workspace_id=request.workspace_id,
            scan_id=scan.id,
            prompt_run_id=run.id if run else None,
            project_id=scan.project_id,
            prompt_set_id=scan.prompt_set_id,
            quota_reservation_id=scan.quota_reservation_id,
            scan_status=ScanStatus(scan.status),
            prompt_run_status=PromptRunStatus(run.status) if run else None,
            usage_event_id=run.usage_event_id if run else None,
            provider_request_id=run.provider_request_id if run else None,
            provider_response_id=run.provider_response_id if run else None,
            web_tool_call_count=run.web_tool_call_count if run else None,
            search_action_count=run.search_action_count if run else None,
            open_page_action_count=run.open_page_action_count if run else None,
            find_in_page_action_count=run.find_in_page_action_count if run else None,
            unknown_web_action_count=run.unknown_web_action_count if run else None,
            cost_usd=_decimal_string(run.cost_usd if run else None),
            calculated_cost_usd=_decimal_string(run.calculated_cost_usd if run else None),
            cost_source=_value(run.cost_source) if run else None,
            pricing_rule_id=run.pricing_rule_id if run else None,
            evidence_dir=resolved_evidence_dir,
            evidence_artifacts=evidence_artifacts,
            evidence_error=evidence_error,
            provider_calls=1 if mode == "EXECUTE_LIVE" else 0,
            celery_dispatches=0,
        )

    @staticmethod
    def _validate_durable_shape(
        scan: Scan,
        project: Project | None,
        prompt_set: PromptSet | None,
        prompts: list[Prompt],
        runs: list[PromptRun],
        prompt: PromptFile | None,
        *,
        require_quota: bool = True,
    ) -> None:
        if project is None or project.id == WHITEBOARDMAKER_PROJECT_ID:
            raise LiveValidationError("Protected or missing validation project.")
        if prompt_set is None or prompt_set.id == WHITEBOARDMAKER_PROMPT_SET_ID:
            raise LiveValidationError("Protected or missing validation PromptSet.")
        if prompt_set.generator_key != LIVE_VALIDATION_GENERATOR_KEY:
            raise LiveValidationError("Validation PromptSet has the wrong generator identity.")
        if scan.prompt_count != 1 or scan.provider_count != 1 or scan.planned_ai_checks != 1:
            raise LiveValidationError("Durable validation plan is not exactly 1x1x1.")
        if require_quota and scan.quota_reservation_id is None:
            raise LiveValidationError("Durable validation plan has no quota reservation.")
        if len(prompts) != 1 or len(runs) != 1:
            raise LiveValidationError("Durable validation plan is not exactly one prompt/run.")
        if runs[0].provider != LLMProvider.OPENAI:
            raise LiveValidationError("Durable validation provider is not OPENAI.")
        if (
            prompt is not None
            and hashlib.sha256(prompts[0].text.encode("utf-8")).hexdigest() != prompt.sha256
        ):
            raise LiveValidationError("validation_id was reused with a different prompt.")

    @staticmethod
    def _scan_key(validation_id: str) -> str:
        return f"{LIVE_VALIDATION_IDEMPOTENCY_PREFIX}:{validation_id}"


def _classify_outcome(run: PromptRun | None, evidence_error: str | None) -> str:
    if run is None:
        return "LIVE VALIDATION BLOCKED — DURABLE STATE MISSING"
    if evidence_error:
        return "LIVE VALIDATION INCONCLUSIVE — EVIDENCE PERSISTENCE FAILURE"
    if run.status == PromptRunStatus.SUCCEEDED:
        if (run.unknown_web_action_count or 0) > 0:
            return "LIVE VALIDATION INCONCLUSIVE — UNKNOWN WEB ACTION"
        if not (run.search_action_count or 0):
            return "LIVE VALIDATION INCONCLUSIVE — SEARCH NOT OBSERVED"
        return "LIVE VALIDATION PASS"
    if run.error_code == ProviderErrorCode.PROVIDER_CONTRACT_VIOLATION:
        return "LIVE VALIDATION FAIL — PROVIDER TOOL BOUND VIOLATION"
    if run.error_code == ProviderErrorCode.SEARCH_ERROR:
        return "LIVE VALIDATION INCONCLUSIVE — SEARCH NOT OBSERVED"
    if run.status == PromptRunStatus.RUNNING:
        return "LIVE VALIDATION BLOCKED — AMBIGUOUS RUNNING STATE"
    return "LIVE VALIDATION INCONCLUSIVE — PROVIDER RESPONSE NOT USABLE"


def _safe_slug(value: str) -> str:
    slug = value.lower().replace("_", "-")
    return slug[:120].strip("-") or "validation"


def _stringify(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _value(value: Any) -> str | None:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _decimal_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _existing_evidence_error(
    evidence_dir: Path,
    *,
    request: LiveValidationRequest,
    prompt: PromptFile,
    scan: Scan,
    run: PromptRun | None,
) -> str | None:
    """Prove that an idempotent result still has its durable evidence."""

    if run is None:
        return "Durable validation evidence has no PromptRun correlation."
    expected_fields: dict[str, Any] = {
        "validation_id": request.validation_id,
        "workspace_id": str(request.workspace_id),
        "project_id": str(scan.project_id),
        "prompt_set_id": str(scan.prompt_set_id),
        "prompt_id": str(run.prompt_id),
        "prompt_sha256": prompt.sha256,
        "scan_id": str(scan.id),
        "prompt_run_id": str(run.id),
        "provider": _value(run.provider),
        "surface": _value(run.provider_surface),
        "requested_model": run.requested_model,
    }
    for field in ("provider_request_id", "provider_response_id"):
        value = getattr(run, field, None)
        if value is not None:
            expected_fields[field] = value
    return validate_evidence_bundle(evidence_dir, expected_fields=expected_fields)
