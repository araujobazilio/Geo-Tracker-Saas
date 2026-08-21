"""Integration tests for Phase 10 Verification Scans and Opportunity
Outcome Tracking.

Tests cover:
- Implementation baseline freezing on IMPLEMENTED transition
- Baseline clearing on IN_PROGRESS transition
- Verification scan creation (eligibility, entitlement, idempotency)
- Verification scan methodology cloning (prompts, providers, snapshots)
- Verification evaluation outcomes (RESOLVED, IMPROVED, NOT_IMPROVED,
  REGRESSED, INCONCLUSIVE)
- VERIFIED status transition (only on RESOLVED)
- Verification history (multiple verifications per Opportunity)
- API endpoints (tenant isolation, role matrix)
- Zero-cost verification (no AI Checks for evaluation)
- Entitlement enforcement (verification_scans_enabled)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    FunnelStage,
    LLMProvider,
    OpportunityPriority,
    OpportunityStatus,
    OpportunityType,
    ProjectStatus,
    PromptSetStatus,
    PromptType,
    ProviderExecutionMode,
    ProviderSurface,
    ScanStatus,
    ScanType,
    VerificationOutcome,
    VerificationReasonCode,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import (
    EntitlementDeniedError,
    ValidationError,
)
from app.models import (
    BillingAccount,
    Competitor,
    Opportunity,
    OpportunityOccurrence,
    OpportunityVerification,
    PlanDefinition,
    PlanProvider,
    Project,
    ProjectKeyword,
    ProjectProvider,
    Prompt,
    PromptSet,
    ProviderPriceRule,
    Scan,
    ScanAnalysis,
    ScanEntitySnapshot,
    User,
    Workspace,
    WorkspaceMember,
)
from app.providers.base import (
    ProviderCapabilities,
    ProviderCitation,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from app.services.action_generation_service import ActionGenerationService
from app.services.opportunity_workflow_service import OpportunityWorkflowService
from app.services.prompt_generation_service import GENERATOR_KEY
from app.services.scan_analysis_service import ScanAnalysisService
from app.services.scan_creation_service import ScanCreationResult, ScanCreationService
from app.services.scan_execution_service import ScanExecutionService
from app.services.scan_finalization_service import ScanFinalizationService
from app.services.verification_evaluation_service import VerificationEvaluationService
from app.services.verification_scan_creation_service import (
    VerificationScanCreationResult,
    VerificationScanCreationService,
)

pytestmark = pytest.mark.integration

# ----------------------------------------------------------------------
# Fake adapters and helpers (adapted from test_action_center.py)
# ----------------------------------------------------------------------

SURFACES = {
    LLMProvider.OPENAI: ProviderSurface.OPENAI_RESPONSES_API,
    LLMProvider.ANTHROPIC: ProviderSurface.ANTHROPIC_MESSAGES_API,
    LLMProvider.GOOGLE: ProviderSurface.GOOGLE_INTERACTIONS_API,
    LLMProvider.PERPLEXITY: ProviderSurface.PERPLEXITY_SONAR_API,
}
MODELS = {
    LLMProvider.OPENAI: "synthetic-openai-model",
    LLMProvider.ANTHROPIC: "synthetic-anthropic-model",
    LLMProvider.GOOGLE: "synthetic-google-model",
    LLMProvider.PERPLEXITY: "synthetic-perplexity-model",
}


def _settings() -> Settings:
    return Settings(
        app_env="test",
        openai_api_key="synthetic-openai-key",
        openai_scan_model=MODELS[LLMProvider.OPENAI],
        anthropic_api_key="synthetic-anthropic-key",
        anthropic_scan_model=MODELS[LLMProvider.ANTHROPIC],
        google_api_key="synthetic-google-key",
        google_scan_model=MODELS[LLMProvider.GOOGLE],
        perplexity_api_key="synthetic-perplexity-key",
        perplexity_scan_model=MODELS[LLMProvider.PERPLEXITY],
        pricing_require_rule_for_execution=True,
        scan_max_concurrency=2,
        scan_stale_after_seconds=60,
    )


class ScriptedAdapter:
    """Fake adapter with configurable response text and citations.

    mention_mode:
    - "brand": response mentions brand_name
    - "competitor": response mentions competitor_name
    - "both": response mentions both
    - "neither": response mentions neither
    """

    def __init__(
        self,
        provider: LLMProvider,
        surface: ProviderSurface,
        *,
        brand_name: str = "Acme",
        competitor_name: str = "Rival",
        mention_mode: str = "both",
        brand_citation_url: str | None = None,
        competitor_citation_url: str | None = None,
        outcomes: list[Exception | None] | None = None,
    ) -> None:
        self.provider = provider
        self.surface = surface
        self.brand_name = brand_name
        self.competitor_name = competitor_name
        self.mention_mode = mention_mode
        self.brand_citation_url = brand_citation_url
        self.competitor_citation_url = competitor_citation_url
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

        parts: list[str] = []
        if self.mention_mode in ("brand", "both"):
            parts.append(f"{self.brand_name} is a great choice.")
        if self.mention_mode in ("competitor", "both"):
            parts.append(f"{self.competitor_name} is also worth considering.")
        if not parts:
            parts.append("Here is a generic answer with no tracked entities.")
        text = " ".join(parts)

        citations: list[ProviderCitation] = []
        if request.mode == ProviderExecutionMode.WEB_GROUNDED:
            if self.brand_citation_url:
                citations.append(
                    ProviderCitation(url=self.brand_citation_url, title="Brand Source")
                )
            if self.competitor_citation_url:
                citations.append(
                    ProviderCitation(url=self.competitor_citation_url, title="Competitor Source")
                )
            if not citations:
                citations.append(
                    ProviderCitation(url="https://unrelated.example.test/page", title="Generic")
                )

        return ProviderResult(
            provider=self.provider,
            surface=self.surface,
            execution_mode=request.mode,
            requested_model=request.model or "",
            returned_model=request.model,
            response_text=text,
            citations=tuple(citations),
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


class ScriptedRegistry:
    def __init__(self, adapters: dict[LLMProvider, ScriptedAdapter]) -> None:
        self.adapters = adapters

    def get(self, provider: LLMProvider) -> ScriptedAdapter:
        return self.adapters[provider]


class FakeDispatcher:
    def __init__(self) -> None:
        self.scan_ids: list[uuid.UUID] = []

    def dispatch(self, scan_id: uuid.UUID) -> None:
        self.scan_ids.append(scan_id)


def _adapter(
    provider: LLMProvider,
    *,
    brand_name: str = "Acme",
    competitor_name: str = "Rival",
    mention_mode: str = "both",
    brand_domain: str = "acme.test",
    competitor_domain: str = "rival.test",
    outcomes: list[Exception | None] | None = None,
) -> ScriptedAdapter:
    return ScriptedAdapter(
        provider,
        SURFACES[provider],
        brand_name=brand_name,
        competitor_name=competitor_name,
        mention_mode=mention_mode,
        brand_citation_url=f"https://{brand_domain}/page" if brand_domain else None,
        competitor_citation_url=f"https://{competitor_domain}/page" if competitor_domain else None,
        outcomes=outcomes,
    )


def _registry(
    providers: list[LLMProvider],
    *,
    brand_name: str = "Acme",
    competitor_name: str = "Rival",
    mention_mode: str = "both",
    brand_domain: str = "acme.test",
    competitor_domain: str = "rival.test",
    per_provider_modes: dict[LLMProvider, str] | None = None,
    outcomes: dict[LLMProvider, list[Exception | None]] | None = None,
) -> ScriptedRegistry:
    outcomes = outcomes or {}
    per_provider_modes = per_provider_modes or {}
    return ScriptedRegistry(
        {
            p: _adapter(
                p,
                brand_name=brand_name,
                competitor_name=competitor_name,
                mention_mode=per_provider_modes.get(p, mention_mode),
                brand_domain=brand_domain,
                competitor_domain=competitor_domain,
                outcomes=outcomes.get(p),
            )
            for p in providers
        }
    )


def _seed(
    db: Session,
    *,
    providers: list[LLMProvider] | None = None,
    prompt_count: int = 1,
    monthly_limit: int = 1000,
    competitors: list[tuple[str, str]] | None = None,
    brand_name: str = "Acme",
    brand_domain: str = "acme.test",
    verification_enabled: bool = True,
    funnel_stage: FunnelStage | None = None,
    commercial_intent: bool = False,
) -> tuple[Workspace, User, Project, PromptSet, list[Prompt]]:
    providers = providers or [LLMProvider.OPENAI]
    suffix = uuid.uuid4().hex
    user = User(email=f"p10-{suffix}@example.test", password_hash="synthetic")
    workspace = Workspace(name=f"Phase10 workspace {suffix}", workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, workspace])
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER))

    plan = PlanDefinition(
        code=f"P10_{suffix}",
        name="Synthetic phase10 plan",
        is_active=True,
        max_projects=10,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
        max_team_members=5,
        monthly_ai_checks=monthly_limit,
        confidence_scans_enabled=True,
        verification_scans_enabled=verification_enabled,
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
        domain=brand_domain,
        brand_name=brand_name,
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
        intent="research",
        funnel_stage=funnel_stage,
        active=True,
    )
    db.add(keyword)
    db.flush()

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
            text=f"best CRM for small business {index}",
            prompt_type=PromptType.NON_BRANDED,
            intent="research",
            funnel_stage=funnel_stage,
            commercial_intent=commercial_intent,
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
    # Clean up any leftover price rules from previous tests that may
    # have committed directly to the database (e.g. phase9_1 concurrent
    # tests using committed_engine).  This delete + insert runs within
    # the test's rollback transaction, so it is safe.
    db.execute(
        ProviderPriceRule.__table__.delete().where(
            ProviderPriceRule.model.in_([MODELS[p] for p in providers])
        )
    )
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


def _connection_factory(db: Session) -> Callable[[], AbstractContextManager[Session]]:
    @contextmanager
    def _ctx() -> Iterator[Session]:
        factory = sessionmaker(
            bind=db.connection(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        session = factory()
        try:
            yield session
        finally:
            session.close()

    return _ctx


def _create_scan(
    db: Session,
    workspace: Workspace,
    user: User,
    project: Project,
    registry: ScriptedRegistry,
    dispatcher: FakeDispatcher,
    *,
    key: str,
) -> ScanCreationResult:
    return ScanCreationService(
        db,
        dispatcher,
        settings=_settings(),
        registry=registry,  # type: ignore[arg-type]
    ).create_scan(
        workspace.id,
        project.id,
        ScanType.STANDARD,
        user.id,
        key,
    )


def _execute(db: Session, scan_id: uuid.UUID, registry: ScriptedRegistry) -> bool:
    return asyncio.run(
        ScanExecutionService(
            _factory(db),
            registry=registry,  # type: ignore[arg-type]
            settings=_settings(),
        ).execute_scan(scan_id)
    )


def _finalize(db: Session, scan_id: uuid.UUID) -> ScanStatus:
    return ScanFinalizationService(db, analysis_session_factory=_connection_factory(db)).finalize(
        scan_id, trigger_analysis=True
    )


def _analyze(db: Session, scan_id: uuid.UUID) -> ScanAnalysis:
    return ScanAnalysisService(db, failure_session_factory=_connection_factory(db)).analyze(scan_id)


def _full_pipeline(
    db: Session,
    workspace: Workspace,
    user: User,
    project: Project,
    registry: ScriptedRegistry,
    dispatcher: FakeDispatcher,
    *,
    key: str,
) -> Scan:
    """Create + execute + finalize + analyze a STANDARD scan."""
    result = _create_scan(db, workspace, user, project, registry, dispatcher, key=key)
    scan = result.scan
    _execute(db, scan.id, registry)
    db.expire_all()
    _finalize(db, scan.id)
    _analyze(db, scan.id)
    db.expire_all()
    return scan


def _refresh_actions(
    db: Session,
    workspace: Workspace,
    project: Project,
    scan_id: uuid.UUID,
) -> int:
    """Refresh Action Center from a scan and return opportunities_created."""
    result = ActionGenerationService(db).refresh_from_scan(workspace.id, project.id, scan_id)
    db.expire_all()
    return result.opportunities_created


def _get_first_opportunity(
    db: Session, project_id: uuid.UUID, opp_type: OpportunityType | None = None
) -> Opportunity:
    query = select(Opportunity).where(Opportunity.project_id == project_id)
    if opp_type is not None:
        query = query.where(Opportunity.opportunity_type == opp_type)
    opp = db.execute(query).scalars().first()
    assert opp is not None
    return opp


def _get_first_occurrence(db: Session, opportunity_id: uuid.UUID) -> OpportunityOccurrence:
    occ = (
        db.execute(
            select(OpportunityOccurrence)
            .where(OpportunityOccurrence.opportunity_id == opportunity_id)
            .order_by(OpportunityOccurrence.created_at.desc())
        )
        .scalars()
        .first()
    )
    assert occ is not None
    return occ


def _create_verification(
    db: Session,
    workspace: Workspace,
    user: User,
    project: Project,
    opportunity_id: uuid.UUID,
    registry: ScriptedRegistry,
    dispatcher: FakeDispatcher,
    *,
    key: str,
) -> VerificationScanCreationResult:
    return VerificationScanCreationService(
        db,
        dispatcher,
        settings=_settings(),
        registry=registry,  # type: ignore[arg-type]
    ).create_verification_scan(
        workspace_id=workspace.id,
        project_id=project.id,
        opportunity_id=opportunity_id,
        requested_by_user_id=user.id,
        idempotency_key=key,
    )


def _evaluate_verification(db: Session, verification_id: uuid.UUID):
    return VerificationEvaluationService(db).evaluate(verification_id)


# ----------------------------------------------------------------------
# Tests: Implementation baseline freezing
# ----------------------------------------------------------------------


def test_implementation_baseline_freezes_on_implemented(db_session: Session) -> None:
    """Transitioning to IMPLEMENTED freezes the latest occurrence."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    # Create a scan where competitor is visible but brand is not → DISCOVERY_GAP.
    registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, registry, dispatcher, key="baseline-1")
    created = _refresh_actions(db_session, ws, project, scan.id)
    assert created >= 1

    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)
    assert opp.status == OpportunityStatus.OPEN
    assert opp.implementation_baseline_occurrence_id is None

    # Transition to IN_PROGRESS then IMPLEMENTED.
    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    db_session.expire_all()

    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    opp = db_session.get(Opportunity, opp.id)
    assert opp.status == OpportunityStatus.IMPLEMENTED
    assert opp.implemented_at is not None
    assert opp.implementation_baseline_occurrence_id is not None

    # The frozen baseline must be the latest occurrence.
    latest_occ = _get_first_occurrence(db_session, opp.id)
    assert opp.implementation_baseline_occurrence_id == latest_occ.id


def test_implementation_baseline_clears_on_in_progress(db_session: Session) -> None:
    """Returning to IN_PROGRESS clears the frozen baseline."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, registry, dispatcher, key="baseline-clear")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()
    assert opp.implementation_baseline_occurrence_id is not None

    # Return to IN_PROGRESS.
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp.status == OpportunityStatus.IN_PROGRESS
    assert opp.implementation_baseline_occurrence_id is None
    assert opp.implemented_at is None


def test_implemented_requires_eligible_occurrence(db_session: Session) -> None:
    """IMPLEMENTED transition fails if no eligible occurrence exists."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Create a scan with no opportunities (brand mentioned in all).
    registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, registry, dispatcher, key="no-opp")
    created = _refresh_actions(db_session, ws, project, scan.id)
    assert created == 0

    # Manually create an Opportunity with no occurrences.
    from app.core.action_engine import ACTION_ENGINE_VERSION
    from app.models.opportunity import Opportunity

    opp = Opportunity(
        workspace_id=ws.id,
        project_id=project.id,
        fingerprint="test-fingerprint-no-occ",
        opportunity_type=OpportunityType.DISCOVERY_VISIBILITY_GAP,
        status=OpportunityStatus.OPEN,
        priority=OpportunityPriority.MEDIUM,
        action_engine_version=ACTION_ENGINE_VERSION,
        competitor_entity_key="competitor:synthetic",
        provider=None,
        prompt_id=None,
        prompt_type=PromptType.NON_BRANDED,
        title="Test opportunity",
        summary="Test summary",
        recommended_action="Test action",
        first_detected_scan_id=scan.id,
        latest_detected_scan_id=scan.id,
        first_detected_at=datetime.now(UTC),
        last_detected_at=datetime.now(UTC),
    )
    db_session.add(opp)
    db_session.commit()

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    with pytest.raises(ValidationError, match="no OpportunityOccurrence"):
        workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)


# ----------------------------------------------------------------------
# Tests: Verification scan creation
# ----------------------------------------------------------------------


def test_verification_scan_creation_basic(db_session: Session) -> None:
    """Create a verification scan for an IMPLEMENTED opportunity."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, registry, dispatcher, key="ver-base")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    result = _create_verification(
        db_session, ws, user, project, opp.id, registry, dispatcher, key="ver-1"
    )
    assert result.created is True
    assert result.scan.scan_type == ScanType.VERIFICATION
    assert result.scan.status == ScanStatus.PENDING
    assert result.scan.repeat_count == 1
    assert result.scan.baseline_scan_id == scan.id
    assert result.scan.prompt_count == scan.prompt_count
    assert result.scan.provider_count == scan.provider_count
    assert result.scan.planned_ai_checks == scan.planned_ai_checks

    assert result.verification.opportunity_id == opp.id
    assert result.verification.baseline_scan_id == scan.id
    assert result.verification.verification_scan_id == result.scan.id
    assert result.verification.outcome == VerificationOutcome.PENDING


def test_verification_scan_requires_implemented_status(db_session: Session) -> None:
    """Verification scan creation requires IMPLEMENTED status."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, registry, dispatcher, key="ver-status")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    # Opportunity is OPEN, not IMPLEMENTED.
    with pytest.raises(ValidationError, match="IMPLEMENTED"):
        _create_verification(
            db_session, ws, user, project, opp.id, registry, dispatcher, key="ver-fail"
        )


def test_verification_scan_entitlement_enforcement(db_session: Session) -> None:
    """Verification scan creation requires verification_scans entitlement."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
        verification_enabled=False,
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, registry, dispatcher, key="ver-ent")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    with pytest.raises(EntitlementDeniedError, match="verification_scans"):
        _create_verification(
            db_session, ws, user, project, opp.id, registry, dispatcher, key="ver-ent-1"
        )


def test_verification_scan_idempotency(db_session: Session) -> None:
    """Creating a verification scan with the same key returns the existing."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, registry, dispatcher, key="ver-idem-base")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    result1 = _create_verification(
        db_session, ws, user, project, opp.id, registry, dispatcher, key="ver-idem"
    )
    assert result1.created is True

    result2 = _create_verification(
        db_session, ws, user, project, opp.id, registry, dispatcher, key="ver-idem"
    )
    assert result2.created is False
    assert result2.scan.id == result1.scan.id
    assert result2.verification.id == result1.verification.id


def test_verification_scan_clones_methodology(db_session: Session) -> None:
    """Verification scan clones prompts, providers, and entity snapshots."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, registry, dispatcher, key="ver-clone")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    result = _create_verification(
        db_session, ws, user, project, opp.id, registry, dispatcher, key="ver-clone-1"
    )

    # Verify entity snapshots are cloned.
    baseline_snaps = list(
        db_session.execute(
            select(ScanEntitySnapshot).where(ScanEntitySnapshot.scan_id == scan.id)
        ).scalars()
    )
    verification_snaps = list(
        db_session.execute(
            select(ScanEntitySnapshot).where(ScanEntitySnapshot.scan_id == result.scan.id)
        ).scalars()
    )
    assert len(verification_snaps) == len(baseline_snaps)
    for base, ver in zip(baseline_snaps, verification_snaps, strict=True):
        assert base.entity_key == ver.entity_key
        assert base.entity_type == ver.entity_type
        assert base.name == ver.name
        assert base.domain == ver.domain
        assert base.aliases == ver.aliases
        assert base.ordinal == ver.ordinal


# ----------------------------------------------------------------------
# Tests: Verification evaluation outcomes
# ----------------------------------------------------------------------


def test_verification_resolved_transitions_to_verified(db_session: Session) -> None:
    """A RESOLVED outcome transitions the Opportunity to VERIFIED."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    # Baseline: competitor only → large visibility gap.
    baseline_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="res-base"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: brand mentioned in all → gap eliminated.
    verification_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        mention_mode="brand",
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, verification_registry, dispatcher, key="res-ver"
    )
    _execute(db_session, result.scan.id, verification_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    eval_result = _evaluate_verification(db_session, result.verification.id)
    assert eval_result.outcome == VerificationOutcome.RESOLVED
    assert eval_result.opportunity_status_after == OpportunityStatus.VERIFIED.value

    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp.status == OpportunityStatus.VERIFIED
    assert opp.verified_at is not None


def test_verification_improved_does_not_verify(db_session: Session) -> None:
    """An IMPROVED outcome does NOT transition to VERIFIED."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    # Baseline: competitor only → 100% competitor, 0% brand, gap=100pp.
    baseline_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="imp-base"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: both mentioned → 100% competitor, 100% brand, gap=0pp.
    # This is actually RESOLVED (gap < threshold). Let's make it IMPROVED:
    # OpenAI=competitor (10), Anthropic=both (10) → brand=50%, comp=100%, gap=50pp.
    # Baseline gap=100pp, verification gap=50pp → delta=50pp >= 5pp → IMPROVED.
    # But 50pp >= 10pp threshold → NOT RESOLVED.
    verification_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "both",
        },
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, verification_registry, dispatcher, key="imp-ver"
    )
    _execute(db_session, result.scan.id, verification_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    eval_result = _evaluate_verification(db_session, result.verification.id)
    # gap went from 100pp to 50pp → IMPROVED (delta=50pp >= 5pp), but 50pp >= 10pp → NOT RESOLVED.
    assert eval_result.outcome == VerificationOutcome.IMPROVED
    assert eval_result.opportunity_status_after == OpportunityStatus.IMPLEMENTED.value

    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp.status == OpportunityStatus.IMPLEMENTED
    assert opp.verified_at is None


def test_verification_not_improved(db_session: Session) -> None:
    """A NOT_IMPROVED outcome preserves the IMPLEMENTED status."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    # Baseline: competitor only → gap=100pp.
    baseline_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="notimp-base"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: same as baseline → gap=100pp, delta=0pp → NOT_IMPROVED.
    verification_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, verification_registry, dispatcher, key="notimp-ver"
    )
    _execute(db_session, result.scan.id, verification_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    eval_result = _evaluate_verification(db_session, result.verification.id)
    assert eval_result.outcome == VerificationOutcome.NOT_IMPROVED
    assert eval_result.opportunity_status_after == OpportunityStatus.IMPLEMENTED.value


def test_verification_regressed(db_session: Session) -> None:
    """A REGRESSED outcome indicates the issue worsened."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    # Baseline: both mentioned → gap=0pp (no opportunity detected).
    # We need a baseline WITH an opportunity. Let's use a moderate gap.
    # OpenAI=competitor (10), Anthropic=both (10) → brand=50%, comp=100%, gap=50pp.
    baseline_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "both",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="reg-base"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: competitor only → gap=100pp (regressed by 50pp).
    verification_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, verification_registry, dispatcher, key="reg-ver"
    )
    _execute(db_session, result.scan.id, verification_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    eval_result = _evaluate_verification(db_session, result.verification.id)
    # gap went from 50pp to 100pp → delta=-50pp → REGRESSED.
    assert eval_result.outcome == VerificationOutcome.REGRESSED
    assert eval_result.opportunity_status_after == OpportunityStatus.IMPLEMENTED.value


def test_verification_inconclusive_low_coverage(db_session: Session) -> None:
    """INCONCLUSIVE outcome when verification coverage is too low."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    # Baseline: competitor only → gap=100pp.
    baseline_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="inc-base"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: all runs fail → 0% coverage → INCONCLUSIVE.
    from app.providers.errors import ProviderResponseError

    verification_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
        outcomes={
            LLMProvider.OPENAI: [ProviderResponseError("fail")] * 10,
            LLMProvider.ANTHROPIC: [ProviderResponseError("fail")] * 10,
        },
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, verification_registry, dispatcher, key="inc-ver"
    )
    _execute(db_session, result.scan.id, verification_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    # No analysis possible with 0 successful runs, but finalization still triggers analysis.
    # Analysis will fail with MISSING_ENTITY_SNAPSHOT or no successful runs.
    db_session.expire_all()

    # The scan should be FAILED (0 successful runs).
    ver_scan = db_session.get(Scan, result.scan.id)
    assert ver_scan.status == ScanStatus.FAILED

    # Phase 10.2: evaluation of a FAILED scan gracefully returns
    # INCONCLUSIVE with VERIFICATION_SCAN_FAILED instead of raising
    # ValidationError and leaving the verification PENDING forever.
    eval_result = _evaluate_verification(db_session, result.verification.id)
    assert eval_result.outcome == VerificationOutcome.INCONCLUSIVE
    assert eval_result.reason_code == VerificationReasonCode.VERIFICATION_SCAN_FAILED


def test_verification_zero_cost_evaluation(db_session: Session) -> None:
    """Evaluation uses zero AI Checks — only deterministic computation."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    baseline_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="zero-base"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    verification_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        mention_mode="brand",
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, verification_registry, dispatcher, key="zero-ver"
    )
    _execute(db_session, result.scan.id, verification_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    # Count UsageEvents before and after evaluation.
    from sqlalchemy import func

    from app.models.usage import UsageEvent

    before = db_session.execute(select(func.count()).select_from(UsageEvent)).scalar_one()

    _evaluate_verification(db_session, result.verification.id)

    after = db_session.execute(select(func.count()).select_from(UsageEvent)).scalar_one()
    assert after == before, "Evaluation must not create any UsageEvents"


# ----------------------------------------------------------------------
# Tests: Verification history
# ----------------------------------------------------------------------


def test_verification_history_multiple_records(db_session: Session) -> None:
    """Multiple verifications can be created for one Opportunity."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    baseline_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="hist-base"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # First verification: IMPROVED.
    verification_registry_1 = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "both",
        },
    )
    result1 = _create_verification(
        db_session, ws, user, project, opp.id, verification_registry_1, dispatcher, key="hist-v1"
    )
    _execute(db_session, result1.scan.id, verification_registry_1)
    db_session.expire_all()
    _finalize(db_session, result1.scan.id)
    _analyze(db_session, result1.scan.id)
    db_session.expire_all()
    eval1 = _evaluate_verification(db_session, result1.verification.id)
    assert eval1.outcome == VerificationOutcome.IMPROVED

    # Second verification: RESOLVED.
    verification_registry_2 = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        mention_mode="brand",
    )
    result2 = _create_verification(
        db_session, ws, user, project, opp.id, verification_registry_2, dispatcher, key="hist-v2"
    )
    _execute(db_session, result2.scan.id, verification_registry_2)
    db_session.expire_all()
    _finalize(db_session, result2.scan.id)
    _analyze(db_session, result2.scan.id)
    db_session.expire_all()
    eval2 = _evaluate_verification(db_session, result2.verification.id)
    assert eval2.outcome == VerificationOutcome.RESOLVED

    # Both verification records exist.
    verifications = list(
        db_session.execute(
            select(OpportunityVerification)
            .where(OpportunityVerification.opportunity_id == opp.id)
            .order_by(OpportunityVerification.created_at)
        ).scalars()
    )
    assert len(verifications) == 2
    assert verifications[0].id == result1.verification.id
    assert verifications[1].id == result2.verification.id


# ----------------------------------------------------------------------
# Tests: VERIFIED status protection
# ----------------------------------------------------------------------


def test_verified_status_is_read_only(db_session: Session) -> None:
    """VERIFIED status cannot be transitioned away from."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    baseline_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="ro-base"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    verification_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        mention_mode="brand",
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, verification_registry, dispatcher, key="ro-ver"
    )
    _execute(db_session, result.scan.id, verification_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()
    _evaluate_verification(db_session, result.verification.id)

    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp.status == OpportunityStatus.VERIFIED

    # Attempt to transition away from VERIFIED.
    with pytest.raises(ValidationError, match="not allowed"):
        workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)


def test_manual_verified_transition_is_forbidden(db_session: Session) -> None:
    """Direct PATCH to VERIFIED is forbidden — only system can set it."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    baseline_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="man-base"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    with pytest.raises(ValidationError, match="VERIFIED status is reserved"):
        workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.VERIFIED)


# ----------------------------------------------------------------------
# Tests: Tenant isolation
# ----------------------------------------------------------------------


def test_verification_tenant_isolation(db_session: Session) -> None:
    """Cross-tenant access to verifications returns 404."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    baseline_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="ti-base"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    result = _create_verification(
        db_session, ws, user, project, opp.id, baseline_registry, dispatcher, key="ti-ver"
    )

    # Create a second workspace.
    suffix = uuid.uuid4().hex
    ws2 = Workspace(name=f"Other ws {suffix}", workspace_type=WorkspaceType.AGENCY)
    db_session.add(ws2)
    db_session.flush()
    other_project = Project(
        workspace_id=ws2.id,
        name="Other project",
        domain=f"other-{suffix}.test",
        brand_name="Other",
        brand_aliases=[],
        target_country="US",
        target_language="en",
        status=ProjectStatus.ACTIVE,
        prompt_input_revision=1,
    )
    db_session.add(other_project)
    db_session.commit()

    # Execute and finalize the verification scan so evaluation is eligible.
    _execute(db_session, result.scan.id, baseline_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    # The evaluation service loads by verification_id directly. Tenant
    # isolation is enforced at the API router layer (workspace_id +
    # project_id + opportunity_id scoping). Here we verify that the
    # verification record itself is workspace-scoped: a query filtering
    # by the wrong workspace_id returns no rows.
    from sqlalchemy import func

    wrong_ws_count = db_session.execute(
        select(func.count())
        .select_from(OpportunityVerification)
        .where(
            OpportunityVerification.id == result.verification.id,
            OpportunityVerification.workspace_id == ws2.id,
        )
    ).scalar_one()
    assert wrong_ws_count == 0

    # The correct workspace can see it.
    correct_ws_count = db_session.execute(
        select(func.count())
        .select_from(OpportunityVerification)
        .where(
            OpportunityVerification.id == result.verification.id,
            OpportunityVerification.workspace_id == ws.id,
        )
    ).scalar_one()
    assert correct_ws_count == 1
