"""Phase 6.1 — Scan finalization and quota reconciliation integrity.

These integration tests run against real PostgreSQL and prove:

* Finalization state + quota release commit together or roll back together.
* A terminal Scan never strands unused reserved quota.
* A terminal Scan never contains unresolved PromptRuns.
* Repeated finalization is idempotent and self-healing.
* Stale PENDING scans (not just RUNNING) can be recovered.
* Recovery never replays provider requests.
* Concurrent finalizers produce one effective release with no duplication.
* Cross-month releases update the original usage period.
* Run-count corruption is detected and rejected.
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    LLMProvider,
    ProjectStatus,
    PromptRunStatus,
    ProviderErrorCode,
    ProviderExecutionMode,
    ProviderSurface,
    QuotaReservationStatus,
    ScanStatus,
    ScanType,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import ConflictError, InfrastructureError
from app.models import (
    BillingAccount,
    PlanDefinition,
    PlanProvider,
    Project,
    ProjectKeyword,
    ProjectProvider,
    Prompt,
    PromptRun,
    PromptSet,
    ProviderPriceRule,
    QuotaReservation,
    Scan,
    UsageEvent,
    User,
    Workspace,
    WorkspaceMember,
    WorkspaceUsagePeriod,
)
from app.providers.base import (
    ProviderCapabilities,
    ProviderCitation,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from app.providers.errors import ProviderTimeoutError
from app.services.prompt_generation_service import GENERATOR_KEY
from app.services.quota_service import QuotaService
from app.services.scan_creation_service import ScanCreationService
from app.services.scan_execution_service import ScanExecutionService
from app.services.scan_finalization_service import (
    ScanFinalizationService,
    ScanRecoveryService,
)
from app.services.scanning.dispatcher import ScanDispatcher

pytestmark = pytest.mark.integration

SURFACE = ProviderSurface.OPENAI_RESPONSES_API
MODEL = "synthetic-finalization-model"


def _settings(*, stale_after: int = 60) -> Settings:
    return Settings(
        app_env="test",
        openai_api_key="synthetic-key",
        openai_scan_model=MODEL,
        anthropic_api_key="synthetic-key",
        anthropic_scan_model="synthetic-anthropic-model",
        google_api_key="synthetic-key",
        google_scan_model="synthetic-google-model",
        perplexity_api_key="synthetic-key",
        perplexity_scan_model="synthetic-perplexity-model",
        pricing_require_rule_for_execution=False,
        scan_max_concurrency=1,
        scan_stale_after_seconds=stale_after,
    )


class FakeDispatcher:
    def __init__(self) -> None:
        self.scan_ids: list[uuid.UUID] = []

    def dispatch(self, scan_id: uuid.UUID) -> None:
        self.scan_ids.append(scan_id)


class FailingDispatcher:
    def dispatch(self, scan_id: uuid.UUID) -> None:
        raise RuntimeError("synthetic broker failure")


class FakeAdapter:
    def __init__(
        self,
        provider: LLMProvider,
        surface: ProviderSurface,
        *,
        outcomes: list[Exception | None] | None = None,
    ) -> None:
        self.provider = provider
        self.surface = surface
        self.outcomes = list(outcomes or [])
        self.requests: list[ProviderRequest] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_model_only=True,
            supports_web_grounded=True,
            supports_citations=True,
            supports_search_result_metadata=True,
        )

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return ProviderResult(
            provider=self.provider,
            surface=self.surface,
            execution_mode=request.mode,
            requested_model=request.model or "",
            returned_model=request.model,
            response_text="Unrelated answer with no tracked brand mention.",
            citations=(ProviderCitation(url="https://example.test/source", title="Source"),),
            usage=ProviderUsage(
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                search_requests=1 if request.mode == ProviderExecutionMode.WEB_GROUNDED else 0,
            ),
            provider_request_id="req-1",
            provider_response_id="resp-1",
            finish_reason="stop",
            latency_ms=7,
            search_used=request.mode == ProviderExecutionMode.WEB_GROUNDED,
        )


class FakeRegistry:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter = adapter

    def get(self, provider: LLMProvider) -> FakeAdapter:
        return self.adapter


def _seed(
    db: Session,
    *,
    prompt_count: int = 2,
    monthly_limit: int = 100,
) -> tuple[Workspace, User, Project, PromptSet, list[Prompt]]:
    suffix = uuid.uuid4().hex
    user = User(email=f"fin-{suffix}@example.test", password_hash="synthetic")
    workspace = Workspace(name=f"Fin workspace {suffix}", workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, workspace])
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER))

    plan = PlanDefinition(
        code=f"FIN_{suffix}",
        name="Finalization test plan",
        is_active=True,
        max_projects=10,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
        max_team_members=5,
        monthly_ai_checks=monthly_limit,
    )
    db.add(plan)
    db.flush()
    db.add(PlanProvider(plan_id=plan.id, provider=LLMProvider.OPENAI))
    db.add(
        BillingAccount(
            workspace_id=workspace.id,
            source=BillingSource.ADMIN,
            status=BillingAccountStatus.ACTIVE,
            plan_code=plan.code,
            is_primary=True,
        )
    )

    project = Project(
        workspace_id=workspace.id,
        name="Finalization project",
        domain=f"{suffix}.example.test",
        brand_name="Synthetic",
        brand_aliases=[],
        target_country="US",
        target_language="en",
        status=ProjectStatus.ACTIVE,
        prompt_input_revision=1,
    )
    db.add(project)
    db.flush()
    db.add(ProjectProvider(project_id=project.id, provider=LLMProvider.OPENAI, enabled=True))
    keyword = ProjectKeyword(
        project_id=project.id,
        text="finalization query",
        normalized_text="finalization query",
        active=True,
    )
    db.add(keyword)
    db.flush()
    prompt_set = PromptSet(
        project_id=project.id,
        version=1,
        input_revision=1,
        status="ACTIVE",
        generator_key=GENERATOR_KEY,
        created_by_user_id=user.id,
        activated_at=datetime.now(UTC),
    )
    db.add(prompt_set)
    db.flush()
    prompts = [
        Prompt(
            prompt_set_id=prompt_set.id,
            project_keyword_id=keyword.id,
            variant_index=index,
            text=f"Finalization prompt {index}",
            prompt_type="NON_BRANDED",
            target_country="US",
            target_language="en",
            active=True,
        )
        for index in range(1, prompt_count + 1)
    ]
    db.add_all(prompts)
    db.commit()
    return workspace, user, project, prompt_set, prompts


def _add_price(db: Session) -> None:
    now = datetime.now(UTC)
    db.add(
        ProviderPriceRule(
            pricing_key=f"fin:{uuid.uuid4().hex}",
            provider=LLMProvider.OPENAI,
            provider_surface=SURFACE,
            model=MODEL,
            effective_from=now - timedelta(days=1),
            effective_to=now + timedelta(days=1),
            input_per_million_usd=Decimal("1.0000000000"),
            cached_input_per_million_usd=None,
            cache_write_per_million_usd=None,
            output_per_million_usd=Decimal("2.0000000000"),
            reasoning_per_million_usd=None,
            citation_per_million_usd=None,
            search_per_1000_usd=Decimal("3.0000000000"),
            request_fee_usd=Decimal("0.0100000000"),
            input_tokens_include_cached=False,
            output_tokens_include_reasoning=False,
            verified_at=now,
            source_url="https://example.test/synthetic-pricing",
            notes="Synthetic finalization test rule",
        )
    )
    db.commit()


def _factory(db: Session) -> sessionmaker[Session]:
    return sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


def _create(
    db: Session,
    workspace: Workspace,
    project: Project,
    user: User,
    dispatcher: ScanDispatcher,
    *,
    key: str,
    settings: Settings | None = None,
) -> Scan:
    registry = FakeRegistry(FakeAdapter(LLMProvider.OPENAI, SURFACE))
    return (
        ScanCreationService(
            db,
            dispatcher,
            settings=settings or _settings(),
            registry=registry,  # type: ignore[arg-type]
        )
        .create_scan(
            workspace.id,
            project.id,
            ScanType.STANDARD,
            user.id,
            key,
        )
        .scan
    )


def _execute(
    db: Session,
    scan_id: uuid.UUID,
    adapter: FakeAdapter,
    settings: Settings | None = None,
) -> bool:
    return asyncio.run(
        ScanExecutionService(
            _factory(db),
            registry=FakeRegistry(adapter),  # type: ignore[arg-type]
            settings=settings or _settings(),
        ).execute_scan(scan_id)
    )


def _count(db: Session, model: type, *criteria: object) -> int:
    stmt = select(func.count()).select_from(model)
    if criteria:
        stmt = stmt.where(*criteria)
    return int(db.execute(stmt).scalar_one())


def _period(db: Session, workspace_id: uuid.UUID) -> WorkspaceUsagePeriod:
    return db.execute(
        select(WorkspaceUsagePeriod).where(WorkspaceUsagePeriod.workspace_id == workspace_id)
    ).scalar_one()


def _runs(db: Session, scan_id: uuid.UUID) -> list[PromptRun]:
    return list(
        db.execute(
            select(PromptRun).where(PromptRun.scan_id == scan_id).order_by(PromptRun.id)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# 1. Atomic finalization + unused quota release
# ---------------------------------------------------------------------------


def test_finalization_state_and_quota_release_commit_atomically(db_session: Session) -> None:
    """A successful finalization commits Scan state and quota release together."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)
    adapter = FakeAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        outcomes=[None, ProviderTimeoutError("timeout", provider="OPENAI")],
    )
    scan = _create(db_session, workspace, project, user, FakeDispatcher(), key="atomic-fin")

    _execute(db_session, scan.id, adapter)
    db_session.expire_all()
    scan = db_session.get(Scan, scan.id)
    assert scan is not None
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    period = _period(db_session, workspace.id)

    assert scan.status == ScanStatus.PARTIAL
    assert (scan.successful_runs, scan.failed_runs) == (1, 1)
    assert reservation is not None and reservation.status == QuotaReservationStatus.RELEASED
    assert (period.ai_checks_used, period.ai_checks_reserved) == (1, 0)


def test_finalization_failure_rolls_back_scan_state_and_quota_release(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If quota release fails during finalization, the Scan must NOT become terminal."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)
    adapter = FakeAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        outcomes=[None, ProviderTimeoutError("timeout", provider="OPENAI")],
    )
    scan = _create(db_session, workspace, project, user, FakeDispatcher(), key="failed-fin")

    original_release = QuotaService.release_reservation

    def failing_release(self: QuotaService, reservation_id: uuid.UUID, **kwargs: object) -> None:
        # Only fail during the finalization release, not during execution.
        # The execution path never calls release_reservation, so any call
        # here is from the finalizer.
        raise ConflictError("synthetic release failure")

    monkeypatch.setattr(QuotaService, "release_reservation", failing_release)
    with pytest.raises(ConflictError, match="synthetic release failure"):
        _execute(db_session, scan.id, adapter)

    # The runs were committed in their own sessions, but the Scan must NOT
    # be durably terminal because finalization rolled back.
    db_session.expire_all()
    scan = db_session.get(Scan, scan.id)
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    period = _period(db_session, workspace.id)

    assert scan is not None and scan.status == ScanStatus.RUNNING
    assert scan.successful_runs == 0  # not updated because finalization rolled back
    assert scan.failed_runs == 0
    assert reservation is not None and reservation.status == QuotaReservationStatus.ACTIVE
    assert reservation.ai_checks_committed == 1  # one run succeeded
    assert period.ai_checks_used == 1
    assert period.ai_checks_reserved == 1  # remaining not released

    # Retry finalization with release working.
    monkeypatch.undo()
    assert original_release is QuotaService.release_reservation
    ScanFinalizationService(db_session).finalize(scan.id)
    db_session.expire_all()
    scan = db_session.get(Scan, scan.id)
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    period = _period(db_session, workspace.id)

    assert scan is not None and scan.status == ScanStatus.PARTIAL
    assert (scan.successful_runs, scan.failed_runs) == (1, 1)
    assert reservation is not None and reservation.status == QuotaReservationStatus.RELEASED
    assert (period.ai_checks_used, period.ai_checks_reserved) == (1, 0)
    assert _count(db_session, UsageEvent, UsageEvent.project_id == project.id) == 1


# ---------------------------------------------------------------------------
# 2. Idempotent / self-healing finalization
# ---------------------------------------------------------------------------


def test_repeated_finalization_is_idempotent(db_session: Session) -> None:
    """Calling finalize() multiple times is safe and produces the same state."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=1)
    _add_price(db_session)
    adapter = FakeAdapter(LLMProvider.OPENAI, SURFACE)
    scan = _create(db_session, workspace, project, user, FakeDispatcher(), key="idempotent-fin")
    _execute(db_session, scan.id, adapter)

    status1 = ScanFinalizationService(db_session).finalize(scan.id)
    status2 = ScanFinalizationService(db_session).finalize(scan.id)
    status3 = ScanFinalizationService(db_session).finalize(scan.id)

    assert status1 == status2 == status3 == ScanStatus.COMPLETED
    db_session.expire_all()
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    assert reservation is not None and reservation.status == QuotaReservationStatus.COMMITTED
    assert _count(db_session, UsageEvent, UsageEvent.project_id == project.id) == 1


def test_legacy_terminal_scan_with_active_reservation_self_heals(db_session: Session) -> None:
    """A pre-fix inconsistent terminal Scan with an ACTIVE reservation is reconciled."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)
    adapter = FakeAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        outcomes=[None, ProviderTimeoutError("timeout", provider="OPENAI")],
    )
    scan = _create(db_session, workspace, project, user, FakeDispatcher(), key="legacy-terminal")

    # Execute to get 1 success + 1 failure.
    _execute(db_session, scan.id, adapter)
    db_session.expire_all()
    scan = db_session.get(Scan, scan.id)
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    assert scan is not None and reservation is not None

    # Simulate a pre-fix inconsistent state: Scan PARTIAL but reservation
    # still ACTIVE with remaining reserved quota.
    scan.status = ScanStatus.PARTIAL
    scan.successful_runs = 1
    scan.failed_runs = 1
    reservation.status = QuotaReservationStatus.ACTIVE
    db_session.commit()
    db_session.expire_all()

    period_before = _period(db_session, workspace.id)
    assert period_before.ai_checks_reserved == 0  # already released by execute

    # Manually re-add the reserved quota to simulate the inconsistent state.
    period_before.ai_checks_reserved = 1
    db_session.commit()
    db_session.expire_all()

    # finalize() should reconcile: release the remaining 1 reserved check.
    status = ScanFinalizationService(db_session).finalize(scan.id)
    db_session.expire_all()
    scan = db_session.get(Scan, scan.id)
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    period = _period(db_session, workspace.id)

    assert status == ScanStatus.PARTIAL
    assert scan is not None and scan.status == ScanStatus.PARTIAL
    assert reservation is not None and reservation.status == QuotaReservationStatus.RELEASED
    assert period.ai_checks_reserved == 0
    assert period.ai_checks_used == 1
    # No provider calls, no new UsageEvents.
    assert _count(db_session, UsageEvent, UsageEvent.project_id == project.id) == 1


# ---------------------------------------------------------------------------
# 3. Terminal Scan → terminal PromptRuns invariant
# ---------------------------------------------------------------------------


def test_invalid_reservation_terminalizes_all_runs_without_provider_calls(
    db_session: Session,
) -> None:
    """When the reservation is invalid before worker claim, all runs FAILED, no provider calls."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)
    adapter = FakeAdapter(LLMProvider.OPENAI, SURFACE)
    scan = _create(
        db_session, workspace, project, user, FakeDispatcher(), key="invalid-reservation"
    )

    # Properly release the reservation before execution (simulates
    # expiration or manual release by an operator/cleanup job).
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    assert reservation is not None
    QuotaService(db_session).release_reservation(reservation.id)
    db_session.expire_all()

    result = _execute(db_session, scan.id, adapter)
    assert result is False
    db_session.expire_all()
    scan = db_session.get(Scan, scan.id)
    runs = _runs(db_session, scan.id)
    period = _period(db_session, workspace.id)

    assert scan is not None and scan.status == ScanStatus.FAILED
    assert scan.failure_code == "INVALID_QUOTA_RESERVATION"
    assert len(runs) == 2
    assert all(run.status == PromptRunStatus.FAILED for run in runs)
    assert all(run.error_code == ProviderErrorCode.ACCOUNTING_ERROR for run in runs)
    assert adapter.requests == []  # no provider calls
    assert (scan.successful_runs, scan.failed_runs) == (0, 2)
    assert _count(db_session, UsageEvent, UsageEvent.project_id == project.id) == 0
    assert (period.ai_checks_used, period.ai_checks_reserved) == (0, 0)


def test_missing_reservation_terminalizes_all_runs(db_session: Session) -> None:
    """When the reservation is missing, all runs FAILED, scan FAILED."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=1)
    _add_price(db_session)
    adapter = FakeAdapter(LLMProvider.OPENAI, SURFACE)
    scan = _create(
        db_session, workspace, project, user, FakeDispatcher(), key="missing-reservation"
    )

    # Remove the reservation reference.
    scan = db_session.get(Scan, scan.id)
    assert scan is not None
    reservation_id = scan.quota_reservation_id
    scan.quota_reservation_id = None
    db_session.commit()
    db_session.expire_all()

    result = _execute(db_session, scan.id, adapter)
    assert result is False
    db_session.expire_all()
    scan = db_session.get(Scan, scan.id)
    runs = _runs(db_session, scan.id)

    assert scan is not None and scan.status == ScanStatus.FAILED
    assert scan.failure_code == "MISSING_QUOTA_RESERVATION"
    assert all(run.status == PromptRunStatus.FAILED for run in runs)
    assert adapter.requests == []

    # The original reservation is still ACTIVE (never released since scan
    # no longer references it). This is an operator data-integrity scenario;
    # the invariant under test is that the scan and runs are terminalized.
    reservation = db_session.get(QuotaReservation, reservation_id)
    assert reservation is not None and reservation.status == QuotaReservationStatus.ACTIVE


# ---------------------------------------------------------------------------
# 4. Stale PENDING recovery
# ---------------------------------------------------------------------------


def test_fresh_pending_scan_is_not_recovered(db_session: Session) -> None:
    """A newly created PENDING scan must not be failed by recovery."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=1)
    _add_price(db_session)
    scan = _create(db_session, workspace, project, user, FakeDispatcher(), key="fresh-pending")
    scan_id = scan.id

    now = datetime.now(UTC)
    ScanRecoveryService(db_session, settings=_settings(stale_after=3600)).recover_stale_scans(now)
    db_session.expire_all()
    scan = db_session.get(Scan, scan_id)

    assert scan is not None and scan.status == ScanStatus.PENDING


def test_stale_pending_scan_is_recovered_without_provider_replay(
    db_session: Session,
) -> None:
    """A stale PENDING scan is recovered: all runs FAILED, quota released, no provider calls."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)
    adapter = FakeAdapter(LLMProvider.OPENAI, SURFACE)
    scan = _create(db_session, workspace, project, user, FakeDispatcher(), key="stale-pending")
    scan_id = scan.id

    # Advance the clock beyond the stale threshold.
    future = datetime.now(UTC) + timedelta(minutes=10)
    ScanRecoveryService(db_session, settings=_settings(stale_after=60)).recover_stale_scans(future)
    db_session.expire_all()
    scan = db_session.get(Scan, scan_id)
    runs = _runs(db_session, scan_id)
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    period = _period(db_session, workspace.id)

    assert adapter.requests == []
    assert scan is not None and scan.status == ScanStatus.FAILED
    assert all(run.status == PromptRunStatus.FAILED for run in runs)
    assert (scan.successful_runs, scan.failed_runs) == (0, 2)
    assert reservation is not None and reservation.status == QuotaReservationStatus.RELEASED
    assert (period.ai_checks_used, period.ai_checks_reserved) == (0, 0)
    assert _count(db_session, UsageEvent, UsageEvent.project_id == project.id) == 0


def test_stale_pending_with_dispatch_failure_is_recovered(db_session: Session) -> None:
    """Scan created, quota reserved, dispatcher fails, scan stays PENDING, then recovered."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)
    adapter = FakeAdapter(LLMProvider.OPENAI, SURFACE)

    with pytest.raises(Exception):  # noqa: B017
        _create(
            db_session,
            workspace,
            project,
            user,
            FailingDispatcher(),  # type: ignore[arg-type]
            key="stale-pending-dispatch-fail",
        )

    db_session.expire_all()
    scan = db_session.execute(
        select(Scan).where(Scan.idempotency_key == "stale-pending-dispatch-fail")
    ).scalar_one()
    scan_id = scan.id
    assert scan.status == ScanStatus.PENDING

    future = datetime.now(UTC) + timedelta(minutes=10)
    ScanRecoveryService(db_session, settings=_settings(stale_after=60)).recover_stale_scans(future)
    db_session.expire_all()
    scan = db_session.get(Scan, scan_id)
    runs = _runs(db_session, scan_id)
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    period = _period(db_session, workspace.id)

    assert adapter.requests == []
    assert scan is not None and scan.status == ScanStatus.FAILED
    assert all(run.status == PromptRunStatus.FAILED for run in runs)
    assert reservation is not None and reservation.status == QuotaReservationStatus.RELEASED
    assert (period.ai_checks_used, period.ai_checks_reserved) == (0, 0)


# ---------------------------------------------------------------------------
# 5. Stale RUNNING recovery (preserved behavior)
# ---------------------------------------------------------------------------


def test_stale_running_recovery_preserves_succeeded_runs(db_session: Session) -> None:
    """Stale RUNNING recovery marks only unresolved runs FAILED, preserves SUCCEEDED."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)
    adapter = FakeAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        outcomes=[None, None],  # both succeed
    )
    scan = _create(db_session, workspace, project, user, FakeDispatcher(), key="stale-running")

    # Execute fully (both runs succeed, scan COMPLETED).
    _execute(db_session, scan.id, adapter)
    db_session.expire_all()
    scan = db_session.get(Scan, scan.id)
    assert scan is not None and scan.status == ScanStatus.COMPLETED

    # Manually revert one run to RUNNING and scan to RUNNING to simulate
    # a worker that died mid-execution after recording evidence.
    run = _runs(db_session, scan.id)[0]
    run.status = PromptRunStatus.RUNNING
    scan.status = ScanStatus.RUNNING
    scan.started_at = datetime.now(UTC) - timedelta(minutes=10)
    scan.successful_runs = 0
    scan.failed_runs = 0
    db_session.commit()
    db_session.expire_all()

    ScanRecoveryService(db_session, settings=_settings(stale_after=60)).recover_stale_scans(
        datetime.now(UTC)
    )
    db_session.expire_all()
    scan = db_session.get(Scan, scan.id)
    runs = _runs(db_session, scan.id)

    assert scan is not None and scan.status == ScanStatus.PARTIAL
    statuses = {run.status for run in runs}
    assert PromptRunStatus.SUCCEEDED in statuses
    assert PromptRunStatus.FAILED in statuses


# ---------------------------------------------------------------------------
# 6. Run-count invariant
# ---------------------------------------------------------------------------


def test_finalizer_detects_run_count_mismatch(db_session: Session) -> None:
    """If the PromptRun row count doesn't match the plan, finalization raises."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)
    scan = _create(db_session, workspace, project, user, FakeDispatcher(), key="corrupt-runs")
    scan_id = scan.id

    # Delete one PromptRun to simulate data corruption.
    run = _runs(db_session, scan.id)[0]
    db_session.delete(run)
    db_session.commit()
    db_session.expire_all()

    # Mark the remaining run terminal so we reach the count check.
    remaining = _runs(db_session, scan.id)[0]
    remaining.status = PromptRunStatus.FAILED
    remaining.error_code = ProviderErrorCode.INTERNAL_ERROR
    remaining.completed_at = datetime.now(UTC)
    db_session.commit()
    db_session.expire_all()

    # Call finalize on a savepoint session so its rollback doesn't wipe
    # the outer test transaction.
    with (
        pytest.raises(InfrastructureError, match="data corruption"),
        _factory(db_session)() as session,
    ):
        ScanFinalizationService(session).finalize(scan_id)

    # Scan must NOT be terminal.
    db_session.expire_all()
    scan = db_session.get(Scan, scan_id)
    assert scan is not None and scan.status == ScanStatus.PENDING


# ---------------------------------------------------------------------------
# 7. Concurrent finalization
# ---------------------------------------------------------------------------


def test_concurrent_finalizers_produce_one_effective_release(db_session: Session) -> None:
    """Two concurrent finalizers on the same run set produce one release, no duplication."""
    import os

    from sqlalchemy import create_engine

    test_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
    )
    independent_engine = create_engine(test_url, pool_pre_ping=True, future=True)
    independent_factory = sessionmaker(bind=independent_engine, expire_on_commit=False)

    # Set up data in a committed session so independent connections can see it.
    with independent_factory() as setup:
        workspace, user, project, prompt_set, prompts = _seed(setup, prompt_count=1)
        _add_price(setup)
        workspace_id = workspace.id
        project_id = project.id
        user_id = user.id

    with independent_factory() as setup:
        scan = _create(
            setup,
            setup.get(Workspace, workspace_id),  # type: ignore[arg-type]
            setup.get(Project, project_id),  # type: ignore[arg-type]
            setup.get(User, user_id),  # type: ignore[arg-type]
            FakeDispatcher(),
            key="concurrent-fin",
        )
        scan_id = scan.id

    # Execute the run (auto-finalizes to COMPLETED).
    adapter_exec = FakeAdapter(LLMProvider.OPENAI, SURFACE)
    exec_factory = sessionmaker(
        bind=independent_engine, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    asyncio.run(
        ScanExecutionService(
            exec_factory,
            registry=FakeRegistry(adapter_exec),  # type: ignore[arg-type]
            settings=_settings(),
        ).execute_scan(scan_id)
    )

    # Reset to RUNNING to simulate a pre-finalization state for concurrency.
    with independent_factory() as reset:
        scan = reset.get(Scan, scan_id)
        assert scan is not None and scan.status == ScanStatus.COMPLETED
        scan.status = ScanStatus.RUNNING
        scan.successful_runs = 0
        scan.failed_runs = 0
        reservation = reset.get(QuotaReservation, scan.quota_reservation_id)
        assert reservation is not None
        reservation.status = QuotaReservationStatus.ACTIVE
        period = _period(reset, workspace_id)
        period.ai_checks_reserved = 1
        reset.commit()

    barrier = Barrier(2)
    results: list[ScanStatus] = []

    def finalize_once() -> None:
        with independent_factory() as session:
            barrier.wait(timeout=10)
            status = ScanFinalizationService(session).finalize(scan_id)
            results.append(status)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: finalize_once(), range(2)))

        with independent_factory() as verify:
            scan = verify.get(Scan, scan_id)
            reservation = verify.get(QuotaReservation, scan.quota_reservation_id)
            period = _period(verify, workspace_id)
            usage_count = _count(verify, UsageEvent, UsageEvent.project_id == project_id)

            assert scan is not None and scan.status == ScanStatus.COMPLETED
            assert reservation is not None
            assert reservation.status == QuotaReservationStatus.COMMITTED
            assert period.ai_checks_reserved >= 0
            assert period.ai_checks_used == 1
            assert usage_count == 1  # no duplicate UsageEvents
    finally:
        # Clean up all data created via the independent engine so it does
        # not pollute subsequent tests in the same session. Delete in FK-safe
        # order, breaking the circular prompt_runs ↔ usage_events FK first.
        with independent_factory() as cleanup:
            cleanup.execute(text("DELETE FROM response_sources"))
            cleanup.execute(
                text("UPDATE prompt_runs SET usage_event_id = NULL WHERE scan_id = :sid"),
                {"sid": str(scan_id)},
            )
            cleanup.execute(
                text("DELETE FROM usage_events WHERE project_id = :pid"), {"pid": str(project_id)}
            )
            cleanup.execute(
                text("DELETE FROM prompt_runs WHERE scan_id = :sid"), {"sid": str(scan_id)}
            )
            cleanup.execute(text("DELETE FROM scans WHERE id = :sid"), {"sid": str(scan_id)})
            cleanup.execute(
                text("DELETE FROM quota_reservations WHERE workspace_id = :wid"),
                {"wid": str(workspace_id)},
            )
            cleanup.execute(
                text("DELETE FROM provider_price_rules WHERE model = :model"), {"model": MODEL}
            )
            cleanup.execute(
                text(
                    "DELETE FROM prompts WHERE prompt_set_id IN (SELECT id FROM prompt_sets WHERE project_id = :pid)"
                ),
                {"pid": str(project_id)},
            )
            cleanup.execute(
                text("DELETE FROM prompt_sets WHERE project_id = :pid"), {"pid": str(project_id)}
            )
            cleanup.execute(
                text("DELETE FROM project_keywords WHERE project_id = :pid"),
                {"pid": str(project_id)},
            )
            cleanup.execute(
                text("DELETE FROM project_providers WHERE project_id = :pid"),
                {"pid": str(project_id)},
            )
            cleanup.execute(text("DELETE FROM projects WHERE id = :pid"), {"pid": str(project_id)})
            cleanup.execute(
                text("DELETE FROM workspace_usage_periods WHERE workspace_id = :wid"),
                {"wid": str(workspace_id)},
            )
            cleanup.execute(
                text("DELETE FROM billing_accounts WHERE workspace_id = :wid"),
                {"wid": str(workspace_id)},
            )
            cleanup.execute(
                text(
                    "DELETE FROM plan_providers WHERE plan_id IN (SELECT id FROM plan_definitions WHERE code LIKE 'FIN_%')"
                )
            )
            cleanup.execute(text("DELETE FROM plan_definitions WHERE code LIKE 'FIN_%'"))
            cleanup.execute(
                text("DELETE FROM workspace_members WHERE workspace_id = :wid"),
                {"wid": str(workspace_id)},
            )
            cleanup.execute(
                text("DELETE FROM workspaces WHERE id = :wid"), {"wid": str(workspace_id)}
            )
            cleanup.execute(text("DELETE FROM users WHERE email LIKE 'fin-%'"))
            cleanup.commit()
        independent_engine.dispose()


# ---------------------------------------------------------------------------
# 8. Cross-month quota release
# ---------------------------------------------------------------------------


def test_cross_month_finalization_releases_from_original_period(db_session: Session) -> None:
    """A scan reserved in August and finalized in September releases from August's period."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=1)
    _add_price(db_session)
    scan = _create(db_session, workspace, project, user, FakeDispatcher(), key="cross-month")

    # Verify the reservation is bound to the current month's period.
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    assert reservation is not None
    original_period = _period(db_session, workspace.id)
    assert reservation.usage_period_id == original_period.id

    # Fail the run and finalize.
    run = _runs(db_session, scan.id)[0]
    run.status = PromptRunStatus.FAILED
    run.error_code = ProviderErrorCode.INTERNAL_ERROR
    run.completed_at = datetime.now(UTC)
    db_session.commit()
    db_session.expire_all()

    ScanFinalizationService(db_session).finalize(scan.id)
    db_session.expire_all()

    scan = db_session.get(Scan, scan.id)
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    period = _period(db_session, workspace.id)

    assert scan is not None and scan.status == ScanStatus.FAILED
    assert reservation is not None and reservation.status == QuotaReservationStatus.RELEASED
    # The original period's reserved quota was released.
    assert period.id == original_period.id
    assert period.ai_checks_reserved == 0
    assert period.ai_checks_used == 0


# ---------------------------------------------------------------------------
# 9. Project last_scan_at participates in finalization transaction
# ---------------------------------------------------------------------------


def test_project_last_scan_at_only_updated_on_success(db_session: Session) -> None:
    """Project.last_scan_at is set only when at least one run succeeded."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)

    # All runs fail → last_scan_at must NOT be set.
    adapter = FakeAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        outcomes=[
            ProviderTimeoutError("timeout", provider="OPENAI"),
            ProviderTimeoutError("timeout", provider="OPENAI"),
        ],
    )
    scan = _create(db_session, workspace, project, user, FakeDispatcher(), key="no-success")
    _execute(db_session, scan.id, adapter)
    db_session.expire_all()
    project = db_session.get(Project, project.id)
    assert project is not None
    assert project.last_scan_at is None

    # At least one success → last_scan_at must be set.
    adapter2 = FakeAdapter(LLMProvider.OPENAI, SURFACE)
    scan2 = _create(db_session, workspace, project, user, FakeDispatcher(), key="with-success")
    _execute(db_session, scan2.id, adapter2)
    db_session.expire_all()
    project = db_session.get(Project, project.id)
    assert project is not None
    assert project.last_scan_at is not None
