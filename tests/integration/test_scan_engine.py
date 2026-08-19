"""Real PostgreSQL integration coverage for the Phase 6 scan engine.

All provider behavior is synthetic: these tests exercise production planning,
quota, persistence, execution, finalization, idempotency, and recovery without
making network calls.
"""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    LLMProvider,
    ProjectStatus,
    PromptRunStatus,
    PromptSetStatus,
    PromptType,
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
    ResponseSource,
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
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderBadRequestError,
    ProviderConfigurationError,
    ProviderModeNotAllowedError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderSearchError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.services.prompt_generation_service import GENERATOR_KEY
from app.services.scan_creation_service import ScanCreationService
from app.services.scan_execution_service import ScanExecutionService
from app.services.scan_finalization_service import ScanRecoveryService

pytestmark = pytest.mark.integration


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
            citations=(
                ProviderCitation(url="https://example.test/first", title="First"),
                ProviderCitation(url="https://example.test/second", title="Second"),
            ),
            usage=ProviderUsage(
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                search_requests=1 if request.mode == ProviderExecutionMode.WEB_GROUNDED else 0,
            ),
            provider_request_id=f"request-{len(self.requests)}",
            provider_response_id=f"response-{len(self.requests)}",
            finish_reason="stop",
            latency_ms=7,
            search_used=request.mode == ProviderExecutionMode.WEB_GROUNDED,
        )


class FakeProviderRegistry:
    def __init__(self, adapters: dict[LLMProvider, FakeAdapter]) -> None:
        self.adapters = adapters
        self.get_calls: list[LLMProvider] = []

    def get(self, provider: LLMProvider) -> FakeAdapter:
        self.get_calls.append(provider)
        return self.adapters[provider]


class SyntheticRows:
    def __init__(
        self,
        workspace: Workspace,
        user: User,
        project: Project,
        prompt_set: PromptSet,
        prompts: list[Prompt],
    ) -> None:
        self.workspace = workspace
        self.user = user
        self.project = project
        self.prompt_set = prompt_set
        self.prompts = prompts


SURFACES = {
    LLMProvider.OPENAI: ProviderSurface.OPENAI_RESPONSES_API,
    LLMProvider.GOOGLE: ProviderSurface.GOOGLE_INTERACTIONS_API,
}
MODELS = {
    LLMProvider.OPENAI: "synthetic-openai-model",
    LLMProvider.GOOGLE: "synthetic-google-model",
}


def _settings(*, require_pricing: bool = True, stale_after: int = 60) -> Settings:
    return Settings(
        app_env="test",
        openai_api_key="synthetic-openai-key",
        openai_scan_model=MODELS[LLMProvider.OPENAI],
        anthropic_api_key="synthetic-anthropic-key",
        anthropic_scan_model="synthetic-anthropic-model",
        google_api_key="synthetic-google-key",
        google_scan_model=MODELS[LLMProvider.GOOGLE],
        perplexity_api_key="synthetic-perplexity-key",
        perplexity_scan_model="synthetic-perplexity-model",
        pricing_require_rule_for_execution=require_pricing,
        scan_max_concurrency=1,
        scan_stale_after_seconds=stale_after,
    )


def _adapter(provider: LLMProvider, outcomes: list[Exception | None] | None = None) -> FakeAdapter:
    return FakeAdapter(provider, SURFACES[provider], outcomes=outcomes)


def _registry(
    providers: list[LLMProvider],
    *,
    outcomes: dict[LLMProvider, list[Exception | None]] | None = None,
) -> FakeProviderRegistry:
    outcomes = outcomes or {}
    return FakeProviderRegistry({p: _adapter(p, outcomes.get(p)) for p in providers})


def _seed(
    db: Session,
    *,
    providers: list[LLMProvider] | None = None,
    prompt_count: int = 1,
    stale_prompt_set: bool = False,
    monthly_limit: int = 100,
) -> SyntheticRows:
    providers = providers or [LLMProvider.OPENAI]
    suffix = uuid.uuid4().hex
    user = User(email=f"scan-{suffix}@example.test", password_hash="synthetic")
    workspace = Workspace(name=f"Scan workspace {suffix}", workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, workspace])
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER))

    plan = PlanDefinition(
        code=f"SCAN_{suffix}",
        name="Synthetic scan plan",
        is_active=True,
        max_projects=10,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
        max_team_members=5,
        monthly_ai_checks=monthly_limit,
    )
    db.add(plan)
    db.flush()
    db.add_all([PlanProvider(plan_id=plan.id, provider=provider) for provider in providers])
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
        name="Synthetic project",
        domain=f"{suffix}.example.test",
        brand_name="Synthetic",
        brand_aliases=[],
        target_country="US",
        target_language="en",
        status=ProjectStatus.ACTIVE,
        prompt_input_revision=2 if stale_prompt_set else 1,
    )
    db.add(project)
    db.flush()
    db.add_all(
        [
            ProjectProvider(project_id=project.id, provider=provider, enabled=True)
            for provider in providers
        ]
    )
    keyword = ProjectKeyword(
        project_id=project.id,
        text="synthetic query",
        normalized_text="synthetic query",
        active=True,
    )
    db.add(keyword)
    db.flush()
    prompt_set = PromptSet(
        project_id=project.id,
        version=1,
        input_revision=1,
        status=PromptSetStatus.ACTIVE,
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
            text=f"Synthetic prompt {index}",
            prompt_type=PromptType.NON_BRANDED,
            target_country="US",
            target_language="en",
            active=True,
        )
        for index in range(1, prompt_count + 1)
    ]
    db.add_all(prompts)
    db.commit()
    return SyntheticRows(workspace, user, project, prompt_set, prompts)


def _add_prices(db: Session, providers: list[LLMProvider]) -> None:
    now = datetime.now(UTC)
    db.add_all(
        [
            ProviderPriceRule(
                pricing_key=f"synthetic:{provider.value}:{uuid.uuid4().hex}",
                provider=provider,
                provider_surface=SURFACES[provider],
                model=MODELS[provider],
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
                notes="Exact synthetic integration-test rule",
            )
            for provider in providers
        ]
    )
    db.commit()


def _factory(db: Session) -> sessionmaker[Session]:
    # Production opens a fresh Session for every claim/record/finalize step.
    # SAVEPOINT joining preserves the db_session fixture's outer rollback while
    # retaining those real transaction boundaries (including local rollbacks).
    return sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


def _create(
    db: Session,
    rows: SyntheticRows,
    registry: FakeProviderRegistry,
    dispatcher: FakeDispatcher,
    *,
    key: str,
    settings: Settings | None = None,
):
    return ScanCreationService(
        db,
        dispatcher,
        settings=settings or _settings(),
        registry=registry,  # type: ignore[arg-type]
    ).create_scan(
        rows.workspace.id,
        rows.project.id,
        ScanType.STANDARD,
        rows.user.id,
        key,
    )


def _execute(
    db: Session,
    scan_id: uuid.UUID,
    registry: FakeProviderRegistry,
    settings: Settings | None = None,
) -> bool:
    return asyncio.run(
        ScanExecutionService(
            _factory(db),
            registry=registry,  # type: ignore[arg-type]
            settings=settings or _settings(),
        ).execute_scan(scan_id)
    )


def _count(db: Session, model: type, *criteria: object) -> int:
    statement = select(func.count()).select_from(model)
    if criteria:
        statement = statement.where(*criteria)
    return int(db.execute(statement).scalar_one())


def _period(db: Session, workspace_id: uuid.UUID) -> WorkspaceUsagePeriod:
    return db.execute(
        select(WorkspaceUsagePeriod).where(WorkspaceUsagePeriod.workspace_id == workspace_id)
    ).scalar_one()


def test_standard_policy_snapshot_plan_and_single_reservation(db_session: Session) -> None:
    providers = [LLMProvider.OPENAI, LLMProvider.GOOGLE]
    rows = _seed(db_session, providers=providers, prompt_count=3)
    _add_prices(db_session, providers)
    registry = _registry(providers)
    dispatcher = FakeDispatcher()

    result = _create(db_session, rows, registry, dispatcher, key="snapshot-plan")
    db_session.expire_all()
    scan = db_session.get(Scan, result.scan.id)
    assert scan is not None
    runs = (
        db_session.execute(
            select(PromptRun).where(PromptRun.scan_id == scan.id).order_by(PromptRun.provider)
        )
        .scalars()
        .all()
    )

    assert (scan.prompt_count, scan.provider_count, scan.planned_ai_checks) == (3, 2, 6)
    assert len(runs) == 6
    assert {
        (run.provider, run.provider_surface, run.execution_mode, run.requested_model)
        for run in runs
    } == {
        (
            LLMProvider.OPENAI,
            ProviderSurface.OPENAI_RESPONSES_API,
            ProviderExecutionMode.WEB_GROUNDED,
            MODELS[LLMProvider.OPENAI],
        ),
        (
            LLMProvider.GOOGLE,
            ProviderSurface.GOOGLE_INTERACTIONS_API,
            ProviderExecutionMode.MODEL_ONLY,
            MODELS[LLMProvider.GOOGLE],
        ),
    }
    assert (
        _count(db_session, QuotaReservation, QuotaReservation.workspace_id == rows.workspace.id)
        == 1
    )
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id)
    assert reservation is not None
    assert reservation.ai_checks_reserved == 6
    assert reservation.ai_checks_committed == 0
    assert dispatcher.scan_ids == [scan.id]


def test_same_idempotency_key_has_one_scan_reservation_and_dispatch(db_session: Session) -> None:
    rows = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    first = _create(db_session, rows, registry, dispatcher, key="same-request")
    second = _create(db_session, rows, registry, dispatcher, key="same-request")

    assert first.created is True
    assert second.created is False
    assert second.scan.id == first.scan.id
    assert (
        _count(
            db_session,
            Scan,
            Scan.workspace_id == rows.workspace.id,
            Scan.idempotency_key == "same-request",
        )
        == 1
    )
    assert (
        _count(db_session, QuotaReservation, QuotaReservation.workspace_id == rows.workspace.id)
        == 1
    )
    assert dispatcher.scan_ids == [first.scan.id]


def test_conflicting_request_reusing_idempotency_key_is_rejected(db_session: Session) -> None:
    rows = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()
    _create(db_session, rows, registry, dispatcher, key="conflicting-request")

    with pytest.raises(ConflictError, match="conflicting scan request"):
        ScanCreationService(
            db_session,
            dispatcher,
            settings=_settings(),
            registry=registry,  # type: ignore[arg-type]
        ).create_scan(
            rows.workspace.id,
            rows.project.id,
            ScanType.CONFIDENCE,
            rows.user.id,
            "conflicting-request",
        )

    assert (
        _count(
            db_session,
            Scan,
            Scan.workspace_id == rows.workspace.id,
            Scan.idempotency_key == "conflicting-request",
        )
        == 1
    )
    assert (
        _count(db_session, QuotaReservation, QuotaReservation.workspace_id == rows.workspace.id)
        == 1
    )
    assert len(dispatcher.scan_ids) == 1


def test_stale_prompt_set_fails_before_quota_provider_or_dispatch(db_session: Session) -> None:
    rows = _seed(db_session, stale_prompt_set=True)
    workspace_id = rows.workspace.id
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    with pytest.raises(ConflictError, match="PromptSet is stale"):
        _create(
            db_session,
            rows,
            registry,
            dispatcher,
            key="stale-prompts",
            settings=_settings(require_pricing=False),
        )

    assert registry.get_calls == []
    assert registry.adapters[LLMProvider.OPENAI].requests == []
    assert _count(db_session, Scan, Scan.workspace_id == workspace_id) == 0
    assert _count(db_session, QuotaReservation, QuotaReservation.workspace_id == workspace_id) == 0
    assert dispatcher.scan_ids == []


def test_missing_pricing_fails_before_quota_or_provider_execution(db_session: Session) -> None:
    rows = _seed(db_session)
    workspace_id = rows.workspace.id
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    with pytest.raises(ConflictError, match="No verified price rule"):
        _create(db_session, rows, registry, dispatcher, key="missing-price")

    assert registry.adapters[LLMProvider.OPENAI].requests == []
    assert _count(db_session, Scan, Scan.workspace_id == workspace_id) == 0
    assert _count(db_session, QuotaReservation, QuotaReservation.workspace_id == workspace_id) == 0
    assert dispatcher.scan_ids == []


def test_success_persists_evidence_usage_and_finalizes_quota(db_session: Session) -> None:
    rows = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()
    created = _create(db_session, rows, registry, dispatcher, key="successful-scan")

    assert _execute(db_session, created.scan.id, registry) is True
    db_session.expire_all()
    scan = db_session.get(Scan, created.scan.id)
    run = db_session.execute(
        select(PromptRun).where(PromptRun.scan_id == created.scan.id)
    ).scalar_one()
    sources = (
        db_session.execute(
            select(ResponseSource)
            .where(ResponseSource.prompt_run_id == run.id)
            .order_by(ResponseSource.ordinal)
        )
        .scalars()
        .all()
    )
    period = _period(db_session, rows.workspace.id)
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id if scan else None)

    assert scan is not None and scan.status == ScanStatus.COMPLETED
    assert (scan.successful_runs, scan.failed_runs) == (1, 0)
    assert run.status == PromptRunStatus.SUCCEEDED
    assert run.usage_event_id is not None
    assert run.pricing_rule_id is not None
    assert [(source.ordinal, source.url) for source in sources] == [
        (1, "https://example.test/first"),
        (2, "https://example.test/second"),
    ]
    assert _count(db_session, UsageEvent, UsageEvent.prompt_run_id == run.id) == 1
    assert reservation is not None and reservation.status == QuotaReservationStatus.COMMITTED
    assert reservation.ai_checks_committed == 1
    assert (period.ai_checks_used, period.ai_checks_reserved) == (1, 0)


def test_partial_scan_charges_only_success_and_finishes_partial(db_session: Session) -> None:
    rows = _seed(db_session, prompt_count=2)
    _add_prices(db_session, [LLMProvider.OPENAI])
    timeout = ProviderTimeoutError("synthetic timeout", provider="OPENAI")
    registry = _registry([LLMProvider.OPENAI], outcomes={LLMProvider.OPENAI: [None, timeout]})
    created = _create(db_session, rows, registry, FakeDispatcher(), key="partial-scan")

    _execute(db_session, created.scan.id, registry)
    db_session.expire_all()
    scan = db_session.get(Scan, created.scan.id)
    runs = (
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == created.scan.id))
        .scalars()
        .all()
    )
    period = _period(db_session, rows.workspace.id)

    assert scan is not None and scan.status == ScanStatus.PARTIAL
    assert (scan.successful_runs, scan.failed_runs) == (1, 1)
    assert {run.status for run in runs} == {PromptRunStatus.SUCCEEDED, PromptRunStatus.FAILED}
    failed = next(run for run in runs if run.status == PromptRunStatus.FAILED)
    assert failed.error_code == ProviderErrorCode.TIMEOUT
    assert _count(db_session, UsageEvent, UsageEvent.project_id == rows.project.id) == 1
    assert (period.ai_checks_used, period.ai_checks_reserved) == (1, 0)


def test_all_failed_scan_uses_no_quota_and_has_no_usage_event(db_session: Session) -> None:
    rows = _seed(db_session, prompt_count=2)
    _add_prices(db_session, [LLMProvider.OPENAI])
    failures = [
        ProviderTimeoutError("synthetic timeout one", provider="OPENAI"),
        ProviderTimeoutError("synthetic timeout two", provider="OPENAI"),
    ]
    registry = _registry([LLMProvider.OPENAI], outcomes={LLMProvider.OPENAI: failures})
    created = _create(db_session, rows, registry, FakeDispatcher(), key="failed-scan")

    _execute(db_session, created.scan.id, registry)
    db_session.expire_all()
    scan = db_session.get(Scan, created.scan.id)
    period = _period(db_session, rows.workspace.id)
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id if scan else None)

    assert scan is not None and scan.status == ScanStatus.FAILED
    assert (scan.successful_runs, scan.failed_runs) == (0, 2)
    assert _count(db_session, UsageEvent, UsageEvent.project_id == rows.project.id) == 0
    assert reservation is not None and reservation.status == QuotaReservationStatus.RELEASED
    assert reservation.ai_checks_committed == 0
    assert (period.ai_checks_used, period.ai_checks_reserved) == (0, 0)


def test_duplicate_execute_scan_delivery_calls_provider_only_once_per_plan(
    db_session: Session,
) -> None:
    rows = _seed(db_session, prompt_count=2)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    created = _create(db_session, rows, registry, FakeDispatcher(), key="duplicate-delivery")

    assert _execute(db_session, created.scan.id, registry) is True
    assert _execute(db_session, created.scan.id, registry) is False

    assert len(registry.adapters[LLMProvider.OPENAI].requests) == created.scan.planned_ai_checks
    assert (
        _count(db_session, UsageEvent, UsageEvent.project_id == rows.project.id)
        == created.scan.planned_ai_checks
    )


def test_terminal_prompt_run_is_not_reexecuted(db_session: Session) -> None:
    rows = _seed(db_session, prompt_count=2)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    created = _create(db_session, rows, registry, FakeDispatcher(), key="terminal-run")
    terminal_run = db_session.execute(
        select(PromptRun)
        .where(PromptRun.scan_id == created.scan.id)
        .order_by(PromptRun.id)
        .limit(1)
    ).scalar_one()
    terminal_run.status = PromptRunStatus.FAILED
    terminal_run.error_code = ProviderErrorCode.TIMEOUT
    terminal_run.error_message = "Already terminal before redelivery."
    terminal_run.completed_at = datetime.now(UTC)
    db_session.commit()

    _execute(db_session, created.scan.id, registry)
    db_session.expire_all()
    terminal_run = db_session.get(PromptRun, terminal_run.id)
    scan = db_session.get(Scan, created.scan.id)

    assert len(registry.adapters[LLMProvider.OPENAI].requests) == 1
    assert terminal_run is not None and terminal_run.status == PromptRunStatus.FAILED
    assert terminal_run.error_message == "Already terminal before redelivery."
    assert scan is not None and scan.status == ScanStatus.PARTIAL
    assert _count(db_session, UsageEvent, UsageEvent.project_id == rows.project.id) == 1


def test_stale_recovery_never_calls_provider_and_releases_quota(db_session: Session) -> None:
    rows = _seed(db_session, prompt_count=2)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    created = _create(db_session, rows, registry, FakeDispatcher(), key="stale-recovery")
    scan = db_session.get(Scan, created.scan.id)
    assert scan is not None
    scan.status = ScanStatus.RUNNING
    scan.started_at = datetime.now(UTC) - timedelta(minutes=10)
    db_session.commit()

    recovered = ScanRecoveryService(
        db_session, settings=_settings(stale_after=60)
    ).recover_stale_scans(datetime.now(UTC))
    db_session.expire_all()
    scan = db_session.get(Scan, created.scan.id)
    runs = (
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == created.scan.id))
        .scalars()
        .all()
    )
    period = _period(db_session, rows.workspace.id)
    reservation = db_session.get(QuotaReservation, scan.quota_reservation_id if scan else None)

    assert recovered == 1
    assert registry.adapters[LLMProvider.OPENAI].requests == []
    assert scan is not None and scan.status == ScanStatus.FAILED
    assert all(run.status == PromptRunStatus.FAILED for run in runs)
    assert _count(db_session, UsageEvent, UsageEvent.project_id == rows.project.id) == 0
    assert reservation is not None and reservation.status == QuotaReservationStatus.RELEASED
    assert (period.ai_checks_used, period.ai_checks_reserved) == (0, 0)


def test_dispatch_failure_is_recoverable_without_duplicate_reservation(db_session: Session) -> None:
    rows = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])

    with pytest.raises(InfrastructureError):
        _create(
            db_session,
            rows,
            registry,
            FailingDispatcher(),  # type: ignore[arg-type]
            key="dispatch-recovery",
        )

    db_session.expire_all()
    scan = db_session.execute(
        select(Scan).where(
            Scan.workspace_id == rows.workspace.id,
            Scan.idempotency_key == "dispatch-recovery",
        )
    ).scalar_one()
    dispatcher = FakeDispatcher()
    retried = _create(
        db_session,
        rows,
        registry,
        dispatcher,
        key="dispatch-recovery",
    )

    assert retried.scan.id == scan.id
    assert dispatcher.scan_ids == [scan.id]
    assert _count(db_session, Scan, Scan.id == scan.id) == 1
    assert _count(db_session, QuotaReservation, QuotaReservation.project_id == rows.project.id) == 1


def test_created_plan_is_unchanged_by_later_project_configuration(db_session: Session) -> None:
    providers = [LLMProvider.OPENAI, LLMProvider.GOOGLE]
    rows = _seed(db_session, providers=providers, prompt_count=2)
    _add_prices(db_session, providers)
    registry = _registry(providers)
    created = _create(db_session, rows, registry, FakeDispatcher(), key="immutable-plan")
    original_runs = (
        db_session.execute(
            select(PromptRun).where(PromptRun.scan_id == created.scan.id).order_by(PromptRun.id)
        )
        .scalars()
        .all()
    )
    snapshots = {
        (run.prompt_id, run.provider, run.provider_surface, run.execution_mode, run.requested_model)
        for run in original_runs
    }

    for configured in db_session.execute(
        select(ProjectProvider).where(ProjectProvider.project_id == rows.project.id)
    ).scalars():
        configured.enabled = False
    rows.prompt_set.status = PromptSetStatus.SUPERSEDED
    replacement = PromptSet(
        project_id=rows.project.id,
        version=2,
        input_revision=rows.project.prompt_input_revision,
        status=PromptSetStatus.ACTIVE,
        generator_key=GENERATOR_KEY,
        created_by_user_id=rows.user.id,
        activated_at=datetime.now(UTC),
    )
    db_session.add(replacement)
    db_session.commit()
    db_session.expire_all()

    scan = db_session.get(Scan, created.scan.id)
    current_runs = (
        db_session.execute(
            select(PromptRun).where(PromptRun.scan_id == created.scan.id).order_by(PromptRun.id)
        )
        .scalars()
        .all()
    )
    assert scan is not None and scan.prompt_set_id == rows.prompt_set.id
    assert {
        (run.prompt_id, run.provider, run.provider_surface, run.execution_mode, run.requested_model)
        for run in current_runs
    } == snapshots


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            ProviderConfigurationError("bad config", provider="OPENAI"),
            ProviderErrorCode.CONFIGURATION_ERROR,
        ),
        (
            ProviderAuthenticationError("bad auth", provider="OPENAI"),
            ProviderErrorCode.AUTHENTICATION_ERROR,
        ),
        (ProviderRateLimitError("limited", provider="OPENAI"), ProviderErrorCode.RATE_LIMITED),
        (ProviderTimeoutError("timeout", provider="OPENAI"), ProviderErrorCode.TIMEOUT),
        (
            ProviderUnavailableError("down", provider="OPENAI"),
            ProviderErrorCode.PROVIDER_UNAVAILABLE,
        ),
        (
            ProviderBadRequestError("bad request", provider="OPENAI"),
            ProviderErrorCode.INVALID_REQUEST,
        ),
        (
            ProviderResponseError("bad response", provider="OPENAI"),
            ProviderErrorCode.MALFORMED_RESPONSE,
        ),
        (ProviderSearchError("search failed", provider="OPENAI"), ProviderErrorCode.SEARCH_ERROR),
        (
            ProviderModeNotAllowedError("bad mode", provider="OPENAI"),
            ProviderErrorCode.MODE_NOT_ALLOWED,
        ),
    ],
)
def test_provider_error_matrix_consumes_zero_checks(
    db_session: Session,
    error: Exception,
    expected_code: ProviderErrorCode,
) -> None:
    rows = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI], outcomes={LLMProvider.OPENAI: [error]})
    created = _create(
        db_session, rows, registry, FakeDispatcher(), key=f"error-{expected_code.value}"
    )

    _execute(db_session, created.scan.id, registry)
    db_session.expire_all()
    run = db_session.execute(
        select(PromptRun).where(PromptRun.scan_id == created.scan.id)
    ).scalar_one()
    period = _period(db_session, rows.workspace.id)

    assert run.status == PromptRunStatus.FAILED
    assert run.error_code == expected_code
    assert run.response_text is None
    assert _count(db_session, UsageEvent, UsageEvent.prompt_run_id == run.id) == 0
    assert (period.ai_checks_used, period.ai_checks_reserved) == (0, 0)


def test_success_without_brand_text_is_distinct_from_failed_measurement(
    db_session: Session,
) -> None:
    providers = [LLMProvider.OPENAI, LLMProvider.GOOGLE]
    rows = _seed(db_session, providers=providers)
    _add_prices(db_session, providers)
    registry = _registry(
        providers,
        outcomes={LLMProvider.GOOGLE: [ProviderTimeoutError("timeout", provider="GOOGLE")]},
    )
    created = _create(db_session, rows, registry, FakeDispatcher(), key="failure-vs-no-mention")

    _execute(db_session, created.scan.id, registry)
    runs = (
        db_session.execute(
            select(PromptRun)
            .where(PromptRun.scan_id == created.scan.id)
            .order_by(PromptRun.provider)
        )
        .scalars()
        .all()
    )
    succeeded = next(run for run in runs if run.status == PromptRunStatus.SUCCEEDED)
    failed = next(run for run in runs if run.status == PromptRunStatus.FAILED)

    assert succeeded.response_text == "Unrelated answer with no tracked brand mention."
    assert succeeded.usage_event_id is not None
    assert failed.response_text is None
    assert failed.usage_event_id is None


def test_concurrent_same_key_creates_one_scan_and_reservation(db_session: Session) -> None:
    engine = db_session.get_bind().engine
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as setup:
        rows = _seed(setup)
        _add_prices(setup, [LLMProvider.OPENAI])
        workspace_id = rows.workspace.id
        project_id = rows.project.id
        user_id = rows.user.id

    barrier = Barrier(2)
    dispatcher = FakeDispatcher()

    def create_once() -> uuid.UUID:
        with factory() as session:
            barrier.wait(timeout=10)
            result = ScanCreationService(
                session,
                dispatcher,
                settings=_settings(),
                registry=_registry([LLMProvider.OPENAI]),  # type: ignore[arg-type]
            ).create_scan(
                workspace_id,
                project_id,
                ScanType.STANDARD,
                user_id,
                "concurrent-idempotency",
            )
            return result.scan.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        scan_ids = list(executor.map(lambda _: create_once(), range(2)))

    with factory() as verify:
        assert len(set(scan_ids)) == 1
        assert (
            _count(
                verify,
                Scan,
                Scan.workspace_id == workspace_id,
                Scan.idempotency_key == "concurrent-idempotency",
            )
            == 1
        )
        assert _count(verify, QuotaReservation, QuotaReservation.project_id == project_id) == 1
        assert _count(verify, PromptRun, PromptRun.scan_id == scan_ids[0]) == 1
    assert dispatcher.scan_ids == [scan_ids[0]]
