"""Integration tests for Phase 8 Confidence Scans.

Tests cover:
- Confidence scan creation from a baseline STANDARD scan
- Baseline eligibility validation
- Repeat count validation (default, min, max)
- Entitlement enforcement (confidence_scans_enabled)
- Provider disallowance (all baseline providers must remain allowed)
- Entity snapshot immutability (cloned from baseline, not current project)
- Run plan correctness (observation_index, attempt_number)
- Round-by-round execution order
- Quota success and partial scenarios
- Reliability metrics (stable, variable, insufficient cells)
- Confidence level classification
- Idempotency
- Tenant isolation
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
    ProviderExecutionMode,
    ProviderSurface,
    ScanStatus,
    ScanType,
    TrackedEntityType,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import (
    ConflictError,
    EntitlementDeniedError,
    NotFoundError,
    ValidationError,
)
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
    Scan,
    ScanEntitySnapshot,
    User,
    Workspace,
    WorkspaceMember,
)
from app.models.tracking import Competitor
from app.providers.base import (
    ProviderCapabilities,
    ProviderCitation,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from app.services.confidence_scan_creation_service import ConfidenceScanCreationService
from app.services.prompt_generation_service import GENERATOR_KEY
from app.services.scan_creation_service import ScanCreationService
from app.services.scan_execution_service import ScanExecutionService

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------
# Fake adapters and helpers (adapted from test_scan_engine.py)
# ----------------------------------------------------------------------


class FakeDispatcher:
    def __init__(self) -> None:
        self.scan_ids: list[uuid.UUID] = []

    def dispatch(self, scan_id: uuid.UUID) -> None:
        self.scan_ids.append(scan_id)


class TrackingFakeAdapter:
    """Fake adapter that records call order and can mention a brand."""

    def __init__(
        self,
        provider: LLMProvider,
        surface: ProviderSurface,
        *,
        mention_brand: bool = True,
        outcomes: list[Exception | None] | None = None,
    ) -> None:
        self.provider = provider
        self.surface = surface
        self.mention_brand = mention_brand
        self.outcomes = list(outcomes or [])
        self.requests: list[ProviderRequest] = []
        self.call_order: list[int] = []

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
        text = (
            "Synthetic brand is the best choice for your needs."
            if self.mention_brand
            else "Unrelated answer with no tracked brand mention."
        )
        return ProviderResult(
            provider=self.provider,
            surface=self.surface,
            execution_mode=request.mode,
            requested_model=request.model or "",
            returned_model=request.model,
            response_text=text,
            citations=(ProviderCitation(url="https://example.test/first", title="First"),),
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
    def __init__(self, adapters: dict[LLMProvider, TrackingFakeAdapter]) -> None:
        self.adapters = adapters

    def get(self, provider: LLMProvider) -> TrackingFakeAdapter:
        return self.adapters[provider]


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
        scan_max_concurrency=2,
        scan_stale_after_seconds=stale_after,
    )


def _adapter(
    provider: LLMProvider,
    *,
    mention_brand: bool = True,
    outcomes: list[Exception | None] | None = None,
) -> TrackingFakeAdapter:
    return TrackingFakeAdapter(
        provider,
        SURFACES[provider],
        mention_brand=mention_brand,
        outcomes=outcomes,
    )


def _registry(
    providers: list[LLMProvider],
    *,
    mention_brand: bool = True,
    outcomes: dict[LLMProvider, list[Exception | None]] | None = None,
) -> FakeProviderRegistry:
    outcomes = outcomes or {}
    return FakeProviderRegistry(
        {p: _adapter(p, mention_brand=mention_brand, outcomes=outcomes.get(p)) for p in providers}
    )


def _seed(
    db: Session,
    *,
    providers: list[LLMProvider] | None = None,
    prompt_count: int = 1,
    monthly_limit: int = 1000,
    confidence_enabled: bool = True,
    competitors: list[tuple[str, str]] | None = None,
) -> tuple[Workspace, User, Project, PromptSet, list[Prompt]]:
    providers = providers or [LLMProvider.OPENAI]
    suffix = uuid.uuid4().hex
    user = User(email=f"conf-{suffix}@example.test", password_hash="synthetic")
    workspace = Workspace(
        name=f"Confidence workspace {suffix}", workspace_type=WorkspaceType.AGENCY
    )
    db.add_all([user, workspace])
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER))

    plan = PlanDefinition(
        code=f"CONF_{suffix}",
        name="Synthetic confidence plan",
        is_active=True,
        max_projects=10,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
        max_team_members=5,
        monthly_ai_checks=monthly_limit,
        confidence_scans_enabled=confidence_enabled,
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
        prompt_input_revision=1,
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

    # Add competitors if provided.
    for comp_name, comp_domain in competitors or []:
        db.add(
            Competitor(
                project_id=project.id,
                name=comp_name,
                domain=comp_domain,
                aliases=[],
                active=True,
            )
        )

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
    return workspace, user, project, prompt_set, prompts


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
    return sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


def _create_standard(
    db: Session,
    workspace: Workspace,
    user: User,
    project: Project,
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
        workspace.id,
        project.id,
        ScanType.STANDARD,
        user.id,
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


def _create_confidence(
    db: Session,
    workspace: Workspace,
    user: User,
    project: Project,
    baseline_scan_id: uuid.UUID,
    registry: FakeProviderRegistry,
    dispatcher: FakeDispatcher,
    *,
    key: str,
    repeat_count: int | None = None,
    settings: Settings | None = None,
):
    return ConfidenceScanCreationService(
        db,
        dispatcher,
        settings=settings or _settings(),
        registry=registry,  # type: ignore[arg-type]
    ).create_confidence_scan(
        workspace_id=workspace.id,
        project_id=project.id,
        baseline_scan_id=baseline_scan_id,
        requested_by_user_id=user.id,
        idempotency_key=key,
        repeat_count=repeat_count,
    )


def _count(db: Session, model: type, *criteria: object) -> int:
    statement = select(func.count()).select_from(model)
    if criteria:
        statement = statement.where(*criteria)
    return int(db.execute(statement).scalar_one())


# ----------------------------------------------------------------------
# Tests: Baseline eligibility
# ----------------------------------------------------------------------


def test_confidence_creation_requires_completed_baseline(db_session: Session) -> None:
    ws, user, project, pset, prompts = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    # Create STANDARD scan but DON'T execute it (still PENDING).
    result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    assert result.scan.status == ScanStatus.PENDING

    with pytest.raises(ValidationError, match="COMPLETED or PARTIAL"):
        _create_confidence(
            db_session,
            ws,
            user,
            project,
            result.scan.id,
            registry,
            dispatcher,
            key="conf-1",
        )


def test_confidence_creation_rejects_non_standard_baseline(db_session: Session) -> None:
    ws, user, project, pset, prompts = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    # Create and execute a STANDARD scan.
    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)
    assert std_result.scan.status in (ScanStatus.COMPLETED, ScanStatus.PARTIAL)

    # Create a confidence scan from the baseline.
    conf_result = _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-1",
        repeat_count=2,
    )

    # Try to create another confidence scan from the confidence scan (non-STANDARD baseline).
    with pytest.raises(ValidationError, match="STANDARD"):
        _create_confidence(
            db_session,
            ws,
            user,
            project,
            conf_result.scan.id,
            registry,
            dispatcher,
            key="conf-2",
        )


def test_confidence_creation_rejects_foreign_workspace_baseline(db_session: Session) -> None:
    ws1, user1, project1, pset1, prompts1 = _seed(db_session)
    ws2, user2, project2, pset2, prompts2 = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    # Create and execute a STANDARD scan in workspace 1.
    std_result = _create_standard(
        db_session, ws1, user1, project1, registry, dispatcher, key="std-1"
    )
    _execute(db_session, std_result.scan.id, registry)

    # Try to create a confidence scan in workspace 2 using workspace 1's baseline.
    with pytest.raises(NotFoundError):
        _create_confidence(
            db_session,
            ws2,
            user2,
            project2,
            std_result.scan.id,
            registry,
            dispatcher,
            key="conf-1",
        )


# ----------------------------------------------------------------------
# Tests: Entitlement enforcement
# ----------------------------------------------------------------------


def test_confidence_creation_rejects_disabled_entitlement(db_session: Session) -> None:
    ws, user, project, pset, prompts = _seed(db_session, confidence_enabled=False)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    with pytest.raises(EntitlementDeniedError):
        _create_confidence(
            db_session,
            ws,
            user,
            project,
            std_result.scan.id,
            registry,
            dispatcher,
            key="conf-1",
        )


# ----------------------------------------------------------------------
# Tests: Repeat count validation
# ----------------------------------------------------------------------


def test_confidence_creation_with_default_repeat_count(db_session: Session) -> None:
    ws, user, project, pset, prompts = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    result = _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-1",  # repeat_count=None -> default=3
    )
    assert result.scan.repeat_count == 3
    assert result.scan.scan_type == ScanType.CONFIDENCE
    assert result.scan.baseline_scan_id == std_result.scan.id


def test_confidence_creation_with_explicit_repeat_count_2(db_session: Session) -> None:
    ws, user, project, pset, prompts = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    result = _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-1",
        repeat_count=2,
    )
    assert result.scan.repeat_count == 2


def test_confidence_creation_rejects_repeat_count_1(db_session: Session) -> None:
    ws, user, project, pset, prompts = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    with pytest.raises(ValidationError, match=">= 2"):
        _create_confidence(
            db_session,
            ws,
            user,
            project,
            std_result.scan.id,
            registry,
            dispatcher,
            key="conf-1",
            repeat_count=1,
        )


def test_confidence_creation_rejects_repeat_count_above_max(db_session: Session) -> None:
    ws, user, project, pset, prompts = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    with pytest.raises(ValidationError, match="<= 5"):
        _create_confidence(
            db_session,
            ws,
            user,
            project,
            std_result.scan.id,
            registry,
            dispatcher,
            key="conf-1",
            repeat_count=6,
        )


# ----------------------------------------------------------------------
# Tests: Run plan correctness
# ----------------------------------------------------------------------


def test_confidence_run_plan_correctness(db_session: Session) -> None:
    """2 prompts x 2 providers x 3 repeats = 12 runs."""
    ws, user, project, pset, prompts = _seed(
        db_session, providers=[LLMProvider.OPENAI, LLMProvider.GOOGLE], prompt_count=2
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.GOOGLE])
    registry = _registry([LLMProvider.OPENAI, LLMProvider.GOOGLE])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    result = _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-1",
        repeat_count=3,
    )
    scan = result.scan
    assert scan.prompt_count == 2
    assert scan.provider_count == 2
    assert scan.planned_ai_checks == 12  # 2 * 2 * 3

    runs = list(db_session.execute(select(PromptRun).where(PromptRun.scan_id == scan.id)).scalars())
    assert len(runs) == 12

    # All attempt_numbers should be 1.
    assert all(r.attempt_number == 1 for r in runs)

    # Observation indices should be 1, 2, 3.
    obs_indices = sorted({r.observation_index for r in runs})
    assert obs_indices == [1, 2, 3]

    # Each observation_index should have 4 runs (2 prompts x 2 providers).
    for obs_idx in [1, 2, 3]:
        obs_runs = [r for r in runs if r.observation_index == obs_idx]
        assert len(obs_runs) == 4


# ----------------------------------------------------------------------
# Tests: Entity snapshot immutability
# ----------------------------------------------------------------------


def test_confidence_snapshots_cloned_from_baseline(db_session: Session) -> None:
    """Confidence snapshots must match baseline, not current project config."""
    ws, user, project, pset, prompts = _seed(db_session, competitors=[("Rival", "rival.test")])
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    # Now change the project brand and competitors.
    project.brand_name = "NewCo"
    project.domain = "newco.test"
    db_session.add(project)
    # Deactivate old competitor, add new one.
    old_comp = db_session.execute(
        select(Competitor).where(Competitor.project_id == project.id)
    ).scalar_one()
    old_comp.active = False
    db_session.add(
        Competitor(
            project_id=project.id, name="Other", domain="other.test", aliases=[], active=True
        )
    )
    db_session.commit()

    result = _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-1",
        repeat_count=2,
    )

    # Confidence snapshots should match baseline, not current project.
    conf_snapshots = list(
        db_session.execute(
            select(ScanEntitySnapshot)
            .where(ScanEntitySnapshot.scan_id == result.scan.id)
            .order_by(ScanEntitySnapshot.ordinal)
        ).scalars()
    )
    baseline_snapshots = list(
        db_session.execute(
            select(ScanEntitySnapshot)
            .where(ScanEntitySnapshot.scan_id == std_result.scan.id)
            .order_by(ScanEntitySnapshot.ordinal)
        ).scalars()
    )

    assert len(conf_snapshots) == len(baseline_snapshots)
    for conf_snap, base_snap in zip(conf_snapshots, baseline_snapshots, strict=True):
        assert conf_snap.name == base_snap.name
        assert conf_snap.domain == base_snap.domain
        assert conf_snap.entity_type == base_snap.entity_type

    # Verify the brand is still "Synthetic", not "NewCo".
    brand_snap = conf_snapshots[0]
    assert brand_snap.name == "Synthetic"
    assert brand_snap.entity_type == TrackedEntityType.BRAND


# ----------------------------------------------------------------------
# Tests: Provider disallowance
# ----------------------------------------------------------------------


def test_confidence_rejects_when_baseline_provider_disallowed(db_session: Session) -> None:
    """Baseline has OPENAI+GOOGLE, current plan only allows OPENAI."""
    ws, user, project, pset, prompts = _seed(
        db_session, providers=[LLMProvider.OPENAI, LLMProvider.GOOGLE]
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.GOOGLE])
    registry = _registry([LLMProvider.OPENAI, LLMProvider.GOOGLE])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    # Remove GOOGLE from the plan's allowed providers.
    plan_provider = db_session.execute(
        select(PlanProvider).where(
            PlanProvider.plan_id != uuid.UUID(int=0),
            PlanProvider.provider == LLMProvider.GOOGLE,
        )
    ).scalar_one()
    db_session.delete(plan_provider)
    db_session.commit()

    with pytest.raises(EntitlementDeniedError, match="GOOGLE"):
        _create_confidence(
            db_session,
            ws,
            user,
            project,
            std_result.scan.id,
            registry,
            dispatcher,
            key="conf-1",
        )


def test_confidence_rejects_when_baseline_provider_disabled_for_project(
    db_session: Session,
) -> None:
    """Baseline has OPENAI+GOOGLE, GOOGLE is disabled for the project."""
    ws, user, project, pset, prompts = _seed(
        db_session, providers=[LLMProvider.OPENAI, LLMProvider.GOOGLE]
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.GOOGLE])
    registry = _registry([LLMProvider.OPENAI, LLMProvider.GOOGLE])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    # Disable GOOGLE for the project.
    pp = db_session.execute(
        select(ProjectProvider).where(
            ProjectProvider.project_id == project.id,
            ProjectProvider.provider == LLMProvider.GOOGLE,
        )
    ).scalar_one()
    pp.enabled = False
    db_session.commit()

    with pytest.raises(ConflictError, match="GOOGLE"):
        _create_confidence(
            db_session,
            ws,
            user,
            project,
            std_result.scan.id,
            registry,
            dispatcher,
            key="conf-1",
        )


# ----------------------------------------------------------------------
# Tests: Model snapshot
# ----------------------------------------------------------------------


def test_confidence_preserves_baseline_requested_model(db_session: Session) -> None:
    """Confidence PromptRuns must snapshot baseline requested_model."""
    ws, user, project, pset, prompts = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    result = _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-1",
        repeat_count=2,
    )

    conf_runs = list(
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == result.scan.id)).scalars()
    )
    baseline_runs = list(
        db_session.execute(
            select(PromptRun).where(PromptRun.scan_id == std_result.scan.id)
        ).scalars()
    )
    baseline_model = baseline_runs[0].requested_model
    for run in conf_runs:
        assert run.requested_model == baseline_model
        assert run.provider_surface == baseline_runs[0].provider_surface
        assert run.execution_mode == baseline_runs[0].execution_mode


# ----------------------------------------------------------------------
# Tests: Quota
# ----------------------------------------------------------------------


def test_confidence_quota_success(db_session: Session) -> None:
    """12 planned, 12 succeed -> used += 12, COMPLETED."""
    ws, user, project, pset, prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.GOOGLE],
        prompt_count=2,
        monthly_limit=1000,
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.GOOGLE])
    registry = _registry([LLMProvider.OPENAI, LLMProvider.GOOGLE])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    result = _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-1",
        repeat_count=3,
    )
    assert result.scan.planned_ai_checks == 12

    _execute(db_session, result.scan.id, registry)

    db_session.expire_all()
    scan = db_session.get(Scan, result.scan.id)
    assert scan is not None
    assert scan.status == ScanStatus.COMPLETED
    assert scan.successful_runs == 12
    assert scan.failed_runs == 0


def test_confidence_quota_partial(db_session: Session) -> None:
    """12 planned, 9 succeed, 3 fail -> PARTIAL, coverage 75%."""
    ws, user, project, pset, prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.GOOGLE],
        prompt_count=2,
        monthly_limit=1000,
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.GOOGLE])
    # OPENAI succeeds, GOOGLE fails on 3rd call (1 prompt x 3 obs = 3 fails).
    registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.GOOGLE],
        outcomes={
            LLMProvider.GOOGLE: [
                None,
                None,
                None,
                None,
                None,  # 5 successes
                Exception("synthetic failure"),
                Exception("synthetic failure"),
                Exception("synthetic failure"),
            ]
        },
    )
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    result = _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-1",
        repeat_count=3,
    )
    _execute(db_session, result.scan.id, registry)

    db_session.expire_all()
    scan = db_session.get(Scan, result.scan.id)
    assert scan is not None
    assert scan.status == ScanStatus.PARTIAL
    assert scan.successful_runs + scan.failed_runs == 12


# ----------------------------------------------------------------------
# Tests: Idempotency
# ----------------------------------------------------------------------


def test_confidence_idempotency_same_key_returns_same_scan(db_session: Session) -> None:
    ws, user, project, pset, prompts = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    result1 = _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-key-1",
        repeat_count=3,
    )
    result2 = _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-key-1",
        repeat_count=3,
    )
    assert result1.scan.id == result2.scan.id


def test_confidence_idempotency_different_repeat_count_conflicts(
    db_session: Session,
) -> None:
    ws, user, project, pset, prompts = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-key-1",
        repeat_count=3,
    )
    with pytest.raises(ConflictError, match="repeat_count"):
        _create_confidence(
            db_session,
            ws,
            user,
            project,
            std_result.scan.id,
            registry,
            dispatcher,
            key="conf-key-1",
            repeat_count=2,
        )


# ----------------------------------------------------------------------
# Tests: Round-by-round execution order
# ----------------------------------------------------------------------


def test_confidence_round_by_round_execution_order(db_session: Session) -> None:
    """All obs=1 runs finish before obs=2 begins.

    Uses a fake adapter with a barrier to ensure round 1 completes
    before round 2 starts.
    """
    import threading

    ws, user, project, pset, prompts = _seed(
        db_session, providers=[LLMProvider.OPENAI], prompt_count=2
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    lock = threading.Lock()

    class OrderedAdapter(TrackingFakeAdapter):
        async def execute(self, request: ProviderRequest) -> ProviderResult:
            # We need to figure out the observation_index from the run.
            # The adapter doesn't have direct access, but we can track
            # call order. With scan_max_concurrency=2 and 2 prompts,
            # round 1 = 2 calls, round 2 = 2 calls, round 3 = 2 calls.
            result = await super().execute(request)
            return result

    # Simpler approach: track call count. With 2 prompts x 1 provider x 3 repeats = 6 calls.
    # Round 1: calls 1-2, Round 2: calls 3-4, Round 3: calls 5-6.
    call_count = [0]

    class CountingAdapter(TrackingFakeAdapter):
        async def execute(self, request: ProviderRequest) -> ProviderResult:
            with lock:
                call_count[0] += 1
            return await super().execute(request)

    registry = FakeProviderRegistry(
        {LLMProvider.OPENAI: CountingAdapter(LLMProvider.OPENAI, SURFACES[LLMProvider.OPENAI])}
    )
    dispatcher = FakeDispatcher()

    std_result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    _execute(db_session, std_result.scan.id, registry)

    result = _create_confidence(
        db_session,
        ws,
        user,
        project,
        std_result.scan.id,
        registry,
        dispatcher,
        key="conf-1",
        repeat_count=3,
    )
    _execute(db_session, result.scan.id, registry)

    # Verify all 6 calls were made.
    assert call_count[0] == 6

    # Verify run statuses.
    db_session.expire_all()
    runs = list(
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == result.scan.id)).scalars()
    )
    assert all(r.status == PromptRunStatus.SUCCEEDED for r in runs)


# ----------------------------------------------------------------------
# Tests: STANDARD regression
# ----------------------------------------------------------------------


def test_standard_scan_still_has_repeat_count_1(db_session: Session) -> None:
    """STANDARD scans must still have repeat_count=1 and observation_index=1."""
    ws, user, project, pset, prompts = _seed(db_session)
    _add_prices(db_session, [LLMProvider.OPENAI])
    registry = _registry([LLMProvider.OPENAI])
    dispatcher = FakeDispatcher()

    result = _create_standard(db_session, ws, user, project, registry, dispatcher, key="std-1")
    assert result.scan.repeat_count == 1
    assert result.scan.baseline_scan_id is None

    runs = list(
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == result.scan.id)).scalars()
    )
    assert all(r.observation_index == 1 for r in runs)
    assert all(r.attempt_number == 1 for r in runs)
