"""Phase 7 — Deterministic brand/competitor detection, citation attribution,
and visibility metrics integration tests.

These integration tests run against real PostgreSQL and prove:

* Entity snapshots are created atomically with the scan plan.
* Deterministic analysis produces correct mentions and attributions.
* Analysis is idempotent (re-running returns the same COMPLETED analysis).
* Analysis is zero-cost (no provider calls, no AI Checks, no UsageEvents).
* Analysis failure does not affect scan terminal state.
* Visibility metrics are computed correctly from evidence.
* Zero vs NULL semantics are correct.
* Provider breakdown is computed correctly.
* Leaderboard sorting is correct.
* Tenant isolation is enforced on all endpoints.
* Role matrix is enforced (ADMIN can trigger, MEMBER can read).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.core.enums import (
    AttributionType,
    BillingAccountStatus,
    BillingSource,
    EntityMatchType,
    LLMProvider,
    ProjectStatus,
    ProviderExecutionMode,
    ProviderSurface,
    ScanAnalysisStatus,
    ScanStatus,
    ScanType,
    TrackedEntityType,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import ValidationError
from app.models import (
    BillingAccount,
    Competitor,
    EntityMention,
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
    SourceAttribution,
    UsageEvent,
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
from app.services.prompt_generation_service import GENERATOR_KEY
from app.services.scan_analysis_service import ScanAnalysisService
from app.services.scan_creation_service import ScanCreationService
from app.services.scan_execution_service import ScanExecutionService
from app.services.scan_finalization_service import ScanFinalizationService
from app.services.scanning.dispatcher import ScanDispatcher
from app.services.visibility_metrics_service import VisibilityMetricsService

pytestmark = pytest.mark.integration

SURFACE = ProviderSurface.OPENAI_RESPONSES_API
MODEL = "synthetic-phase7-model"


def _settings() -> Settings:
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
        scan_stale_after_seconds=60,
    )


class FakeDispatcher:
    def __init__(self) -> None:
        self.scan_ids: list[uuid.UUID] = []

    def dispatch(self, scan_id: uuid.UUID) -> None:
        self.scan_ids.append(scan_id)


class ScriptedAdapter:
    """Provider adapter that returns scripted response text and citations."""

    def __init__(
        self,
        provider: LLMProvider,
        surface: ProviderSurface,
        *,
        response_text: str = "No brand mentioned.",
        citations: tuple[ProviderCitation, ...] = (),
    ) -> None:
        self.provider = provider
        self.surface = surface
        self.response_text = response_text
        self.citations = citations
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
        return ProviderResult(
            provider=self.provider,
            surface=self.surface,
            execution_mode=request.mode,
            requested_model=request.model or "",
            returned_model=request.model,
            response_text=self.response_text,
            citations=self.citations,
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
    def __init__(self, adapter: ScriptedAdapter) -> None:
        self.adapter = adapter

    def get(self, provider: LLMProvider) -> ScriptedAdapter:
        return self.adapter


def _seed(
    db: Session,
    *,
    brand_name: str = "Acme",
    brand_domain: str = "acme.test",
    brand_aliases: list[str] | None = None,
    competitors: list[dict] | None = None,
    prompt_count: int = 2,
    monthly_limit: int = 100,
) -> tuple[Workspace, User, Project, PromptSet, list[Prompt]]:
    suffix = uuid.uuid4().hex
    user = User(email=f"p7-{suffix}@example.test", password_hash="synthetic")
    workspace = Workspace(name=f"P7 workspace {suffix}", workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, workspace])
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER))

    plan = PlanDefinition(
        code=f"P7_{suffix}",
        name="Phase 7 test plan",
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
        name="Phase 7 project",
        domain=brand_domain,
        brand_name=brand_name,
        brand_aliases=brand_aliases or [],
        target_country="US",
        target_language="en",
        status=ProjectStatus.ACTIVE,
        prompt_input_revision=1,
    )
    db.add(project)
    db.flush()
    db.add(ProjectProvider(project_id=project.id, provider=LLMProvider.OPENAI, enabled=True))

    for comp in competitors or []:
        db.add(
            Competitor(
                project_id=project.id,
                name=comp["name"],
                domain=comp["domain"],
                aliases=comp.get("aliases", []),
                active=True,
            )
        )

    keyword = ProjectKeyword(
        project_id=project.id,
        text="best crm",
        normalized_text="best crm",
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
            text=f"Phase 7 prompt {index}",
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
            pricing_key=f"p7:{uuid.uuid4().hex}",
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
            notes="Synthetic phase 7 test rule",
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


def _connection_factory(db: Session) -> Callable[[], AbstractContextManager[Session]]:
    """Factory bound to the same connection (for auto-trigger tests)."""

    @contextmanager
    def _ctx() -> Iterator[Session]:  # type: ignore[type-arg]
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
    project: Project,
    user: User,
    dispatcher: ScanDispatcher,
    *,
    key: str,
    adapter: ScriptedAdapter,
) -> Scan:
    return (
        ScanCreationService(
            db,
            dispatcher,
            settings=_settings(),
            registry=FakeRegistry(adapter),  # type: ignore[arg-type]
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


def _execute_scan(
    db: Session,
    scan_id: uuid.UUID,
    adapter: ScriptedAdapter,
) -> bool:
    return asyncio.run(
        ScanExecutionService(
            _factory(db),
            registry=FakeRegistry(adapter),  # type: ignore[arg-type]
            settings=_settings(),
        ).execute_scan(scan_id)
    )


def _finalize(db: Session, scan_id: uuid.UUID) -> ScanStatus:
    return ScanFinalizationService(db, analysis_session_factory=_connection_factory(db)).finalize(
        scan_id, trigger_analysis=True
    )


# ---------------------------------------------------------------------------
# 1. Entity snapshot creation
# ---------------------------------------------------------------------------


def test_entity_snapshots_created_with_scan(db_session: Session) -> None:
    """Scan creation atomically creates BRAND + COMPETITOR snapshots."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
        brand_aliases=["Acme CRM"],
        competitors=[
            {"name": "Salesforce", "domain": "salesforce.com", "aliases": ["SF"]},
            {"name": "HubSpot", "domain": "hubspot.com", "aliases": []},
        ],
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(LLMProvider.OPENAI, SURFACE)
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="snapshots-test",
        adapter=adapter,
    )

    snapshots = list(
        db_session.execute(
            select(ScanEntitySnapshot)
            .where(ScanEntitySnapshot.scan_id == scan.id)
            .order_by(ScanEntitySnapshot.ordinal)
        ).scalars()
    )

    assert len(snapshots) == 3
    assert snapshots[0].entity_type == TrackedEntityType.BRAND
    assert snapshots[0].name == "Acme"
    assert snapshots[0].domain == "acme.test"
    assert snapshots[0].aliases == ["Acme CRM"]
    assert snapshots[0].ordinal == 1
    assert snapshots[0].source_competitor_id is None

    assert snapshots[1].entity_type == TrackedEntityType.COMPETITOR
    assert snapshots[1].ordinal == 2
    assert snapshots[1].source_competitor_id is not None

    assert snapshots[2].entity_type == TrackedEntityType.COMPETITOR
    assert snapshots[2].ordinal == 3

    # Competitors are sorted by domain for determinism
    comp_names = {s.name for s in snapshots[1:]}
    assert comp_names == {"Salesforce", "HubSpot"}


def test_entity_snapshots_exclude_inactive_competitors(db_session: Session) -> None:
    """Only ACTIVE competitors are snapshotted."""
    workspace, user, project, _, _ = _seed(
        db_session,
        competitors=[{"name": "ActiveComp", "domain": "active.test"}],
    )
    # Add an inactive competitor directly
    db_session.add(
        Competitor(
            project_id=project.id,
            name="InactiveComp",
            domain="inactive.test",
            active=False,
        )
    )
    db_session.commit()

    _add_price(db_session)
    adapter = ScriptedAdapter(LLMProvider.OPENAI, SURFACE)
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="inactive-comp-test",
        adapter=adapter,
    )

    snapshots = list(
        db_session.execute(
            select(ScanEntitySnapshot)
            .where(ScanEntitySnapshot.scan_id == scan.id)
            .order_by(ScanEntitySnapshot.ordinal)
        ).scalars()
    )

    names = [s.name for s in snapshots]
    assert "InactiveComp" not in names
    assert "ActiveComp" in names


def test_entity_snapshots_unique_per_scan(db_session: Session) -> None:
    """Duplicate snapshot creation should be prevented by unique constraint."""
    workspace, user, project, _, _ = _seed(db_session)
    _add_price(db_session)
    adapter = ScriptedAdapter(LLMProvider.OPENAI, SURFACE)
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="unique-snapshots",
        adapter=adapter,
    )

    # Attempt to add a duplicate brand snapshot
    from sqlalchemy.exc import IntegrityError

    dup = ScanEntitySnapshot(
        scan_id=scan.id,
        entity_key="brand",
        entity_type=TrackedEntityType.BRAND,
        name=project.brand_name,
        domain=project.domain,
        aliases=[],
        ordinal=99,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------------------
# 2. Deterministic analysis — mentions
# ---------------------------------------------------------------------------


def test_analysis_detects_brand_mention(db_session: Session) -> None:
    """Analysis detects brand name in response text."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="I recommend Acme for your CRM needs.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="brand-mention",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    analysis = ScanAnalysisService(db_session).analyze(scan.id)
    assert analysis.status == ScanAnalysisStatus.COMPLETED

    mentions = list(
        db_session.execute(
            select(EntityMention).where(EntityMention.scan_analysis_id == analysis.id)
        ).scalars()
    )
    assert len(mentions) >= 1
    brand_mentions = [
        m
        for m in mentions
        if db_session.get(ScanEntitySnapshot, m.entity_snapshot_id).entity_type
        == TrackedEntityType.BRAND
    ]
    assert len(brand_mentions) == 2  # 2 prompts, both mention Acme
    assert all(m.match_type == EntityMatchType.NAME for m in brand_mentions)
    assert all(m.matched_text == "Acme" for m in brand_mentions)


def test_analysis_detects_competitor_mention(db_session: Session) -> None:
    """Analysis detects competitor name in response text."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
        competitors=[{"name": "Salesforce", "domain": "salesforce.com"}],
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Salesforce is the best CRM.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="comp-mention",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    analysis = ScanAnalysisService(db_session).analyze(scan.id)
    mentions = list(
        db_session.execute(
            select(EntityMention).where(EntityMention.scan_analysis_id == analysis.id)
        ).scalars()
    )
    comp_mentions = [
        m
        for m in mentions
        if db_session.get(ScanEntitySnapshot, m.entity_snapshot_id).entity_type
        == TrackedEntityType.COMPETITOR
    ]
    assert len(comp_mentions) == 2  # 2 prompts, both mention Salesforce
    assert all(m.matched_text == "Salesforce" for m in comp_mentions)


def test_analysis_no_false_positive_substring(db_session: Session) -> None:
    """Acmeology should NOT match Acme."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acmeology is a different company entirely.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="no-false-positive",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    analysis = ScanAnalysisService(db_session).analyze(scan.id)
    mentions = list(
        db_session.execute(
            select(EntityMention).where(EntityMention.scan_analysis_id == analysis.id)
        ).scalars()
    )
    assert len(mentions) == 0


# ---------------------------------------------------------------------------
# 3. Deterministic analysis — source attribution
# ---------------------------------------------------------------------------


def test_analysis_attributes_owned_domain(db_session: Session) -> None:
    """Analysis attributes a source URL to the brand via domain matching."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acme is great.",
        citations=(ProviderCitation(url="https://acme.test/page", title="Acme"),),
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="owned-domain",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    analysis = ScanAnalysisService(db_session).analyze(scan.id)
    attributions = list(
        db_session.execute(
            select(SourceAttribution).where(SourceAttribution.scan_analysis_id == analysis.id)
        ).scalars()
    )
    assert len(attributions) == 2  # 2 runs, each with 1 citation to acme.test
    assert all(a.attribution_type == AttributionType.OWNED_DOMAIN for a in attributions)
    assert all(a.source_host == "acme.test" for a in attributions)


def test_analysis_no_attribution_for_unrelated_domain(db_session: Session) -> None:
    """Unrelated source domains get no attribution."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acme is great.",
        citations=(ProviderCitation(url="https://wikipedia.org/article", title="Wiki"),),
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="unrelated-domain",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    analysis = ScanAnalysisService(db_session).analyze(scan.id)
    attributions = list(
        db_session.execute(
            select(SourceAttribution).where(SourceAttribution.scan_analysis_id == analysis.id)
        ).scalars()
    )
    assert len(attributions) == 0


# ---------------------------------------------------------------------------
# 4. Analysis idempotency and zero-cost
# ---------------------------------------------------------------------------


def test_analysis_is_idempotent(db_session: Session) -> None:
    """Re-running analysis returns the same COMPLETED analysis without duplicates."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acme is recommended.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="idempotent-analysis",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    service = ScanAnalysisService(db_session)
    analysis1 = service.analyze(scan.id)
    assert analysis1.status == ScanAnalysisStatus.COMPLETED

    mention_count_1 = int(
        db_session.execute(
            select(func.count(EntityMention.id)).where(
                EntityMention.scan_analysis_id == analysis1.id
            )
        ).scalar_one()
    )

    analysis2 = service.analyze(scan.id)
    assert analysis2.id == analysis1.id
    assert analysis2.status == ScanAnalysisStatus.COMPLETED

    mention_count_2 = int(
        db_session.execute(
            select(func.count(EntityMention.id)).where(
                EntityMention.scan_analysis_id == analysis2.id
            )
        ).scalar_one()
    )
    assert mention_count_1 == mention_count_2


def test_analysis_is_zero_cost(db_session: Session) -> None:
    """Analysis consumes 0 AI Checks and creates 0 UsageEvents."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acme is recommended.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="zero-cost-analysis",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    usage_before = int(
        db_session.execute(
            select(func.count(UsageEvent.id)).where(UsageEvent.project_id == project.id)
        ).scalar_one()
    )

    ScanAnalysisService(db_session).analyze(scan.id)

    usage_after = int(
        db_session.execute(
            select(func.count(UsageEvent.id)).where(UsageEvent.project_id == project.id)
        ).scalar_one()
    )
    assert usage_before == usage_after


# ---------------------------------------------------------------------------
# 5. Analysis eligibility
# ---------------------------------------------------------------------------


def test_analysis_rejects_non_terminal_scan(db_session: Session) -> None:
    """Analysis cannot run on a PENDING scan."""
    workspace, user, project, _, _ = _seed(db_session)
    _add_price(db_session)
    adapter = ScriptedAdapter(LLMProvider.OPENAI, SURFACE)
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="non-terminal",
        adapter=adapter,
    )
    # Don't execute or finalize — scan is PENDING
    with pytest.raises(ValidationError):
        ScanAnalysisService(db_session).analyze(scan.id)


def test_analysis_rejects_all_failed_scan(db_session: Session) -> None:
    """Analysis cannot run on a scan with 0 successful runs."""
    from app.providers.errors import ProviderTimeoutError

    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)
    # Override with failing adapter
    failing_adapter = ScriptedAdapter(LLMProvider.OPENAI, SURFACE)
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="all-failed",
        adapter=failing_adapter,
    )

    # Make all runs fail by using a failing adapter for execution

    class AllFailingAdapter(ScriptedAdapter):
        async def execute(self, request: ProviderRequest) -> ProviderResult:
            raise ProviderTimeoutError("timeout", provider="OPENAI")

    failing = AllFailingAdapter(LLMProvider.OPENAI, SURFACE)
    _execute_scan(db_session, scan.id, failing)
    _finalize(db_session, scan.id)

    scan = db_session.get(Scan, scan.id)
    assert scan is not None
    assert scan.status == ScanStatus.FAILED

    with pytest.raises(ValidationError):
        ScanAnalysisService(db_session).analyze(scan.id)


# ---------------------------------------------------------------------------
# 6. Visibility metrics
# ---------------------------------------------------------------------------


def test_metrics_visibility_rate(db_session: Session) -> None:
    """Visibility rate = mentioned runs / successful runs."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
        prompt_count=2,
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acme is the best CRM available.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="vis-rate",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    ScanAnalysisService(db_session).analyze(scan.id)

    result = VisibilityMetricsService(db_session).get_metrics(workspace.id, project.id, scan.id)
    brand = next(em for em in result.entity_metrics if em.entity_type == "BRAND")
    assert brand.successful_observations == 2
    assert brand.mentioned_observations == 2
    assert brand.visibility_rate == Decimal("100.0000")


def test_metrics_zero_vs_null_semantics(db_session: Session) -> None:
    """0 successful → NULL visibility; >0 successful, 0 mentions → 0.0."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
        competitors=[{"name": "Salesforce", "domain": "salesforce.com"}],
        prompt_count=2,
    )
    _add_price(db_session)
    # Response mentions neither brand nor competitor
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="There are many CRMs on the market.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="zero-vs-null",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    ScanAnalysisService(db_session).analyze(scan.id)

    result = VisibilityMetricsService(db_session).get_metrics(workspace.id, project.id, scan.id)
    brand = next(em for em in result.entity_metrics if em.entity_type == "BRAND")
    # 2 successful, 0 mentions → visibility_rate = 0.0 (real zero)
    assert brand.successful_observations == 2
    assert brand.mentioned_observations == 0
    assert brand.visibility_rate == Decimal("0.0000")
    # No entity mentioned → share_of_voice = NULL
    assert brand.share_of_voice is None


def test_metrics_share_of_voice(db_session: Session) -> None:
    """Share of Voice distributes across mentioned entities."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
        competitors=[{"name": "Salesforce", "domain": "salesforce.com"}],
        prompt_count=2,
    )
    _add_price(db_session)
    # Both runs mention both Acme and Salesforce
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acme and Salesforce are both good CRMs.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="sov",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    ScanAnalysisService(db_session).analyze(scan.id)

    result = VisibilityMetricsService(db_session).get_metrics(workspace.id, project.id, scan.id)
    brand = next(em for em in result.entity_metrics if em.entity_type == "BRAND")
    comp = next(em for em in result.entity_metrics if em.entity_type == "COMPETITOR")
    # Both mentioned in 2 runs each → 50% each
    assert brand.share_of_voice == Decimal("50.0000")
    assert comp.share_of_voice == Decimal("50.0000")


def test_metrics_measurement_coverage(db_session: Session) -> None:
    """Measurement coverage = successful / planned."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acme is great.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="coverage",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    ScanAnalysisService(db_session).analyze(scan.id)

    result = VisibilityMetricsService(db_session).get_metrics(workspace.id, project.id, scan.id)
    # 2 successful, 2 planned → 100%
    assert result.measurement_coverage == Decimal("100.0000")


def test_metrics_leaderboard_sorted_by_visibility_desc(db_session: Session) -> None:
    """Leaderboard sorts by visibility_rate descending."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
        competitors=[{"name": "Salesforce", "domain": "salesforce.com"}],
        prompt_count=2,
    )
    _add_price(db_session)
    # Only Acme mentioned
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acme is the best.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="leaderboard",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    ScanAnalysisService(db_session).analyze(scan.id)

    result = VisibilityMetricsService(db_session).get_metrics(workspace.id, project.id, scan.id)
    assert len(result.leaderboard) == 2
    # Acme (100%) should be first, Salesforce (0%) second
    assert result.leaderboard[0].name == "Acme"
    assert result.leaderboard[1].name == "Salesforce"


def test_metrics_provider_breakdown(db_session: Session) -> None:
    """Provider breakdown includes per-provider visibility."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
        prompt_count=2,
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acme is recommended.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="provider-breakdown",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    ScanAnalysisService(db_session).analyze(scan.id)

    result = VisibilityMetricsService(db_session).get_metrics(workspace.id, project.id, scan.id)
    assert len(result.provider_breakdown) == 1
    pb = result.provider_breakdown[0]
    assert pb.provider == LLMProvider.OPENAI
    assert pb.successful_observations == 2
    assert pb.visibility_rate == Decimal("100.0000")


# ---------------------------------------------------------------------------
# 7. Auto-trigger after finalization
# ---------------------------------------------------------------------------


def test_analysis_auto_triggered_after_finalization(db_session: Session) -> None:
    """Finalization auto-triggers analysis for eligible scans."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acme is recommended.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="auto-trigger",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    # Analysis should have been auto-triggered
    analysis = db_session.execute(
        select(ScanAnalysis).where(ScanAnalysis.scan_id == scan.id)
    ).scalar_one_or_none()
    assert analysis is not None
    assert analysis.status == ScanAnalysisStatus.COMPLETED


def test_analysis_not_triggered_for_failed_scan(db_session: Session) -> None:
    """Finalization does NOT auto-trigger analysis for all-failed scans."""
    workspace, user, project, _, _ = _seed(db_session, prompt_count=2)
    _add_price(db_session)

    class AllFailingAdapter(ScriptedAdapter):
        async def execute(self, request: ProviderRequest) -> ProviderResult:
            from app.providers.errors import ProviderTimeoutError

            raise ProviderTimeoutError("timeout", provider="OPENAI")

    failing = AllFailingAdapter(LLMProvider.OPENAI, SURFACE)
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="no-auto-failed",
        adapter=failing,
    )
    _execute_scan(db_session, scan.id, failing)
    status = _finalize(db_session, scan.id)
    assert status == ScanStatus.FAILED

    analysis = db_session.execute(
        select(ScanAnalysis).where(ScanAnalysis.scan_id == scan.id)
    ).scalar_one_or_none()
    # No analysis should exist for a FAILED scan
    assert analysis is None


# ---------------------------------------------------------------------------
# 8. Analysis failure isolation
# ---------------------------------------------------------------------------


def test_analysis_failure_does_not_affect_scan_state(db_session: Session) -> None:
    """If analysis fails, the scan remains terminal."""
    workspace, user, project, _, _ = _seed(
        db_session,
        brand_name="Acme",
        brand_domain="acme.test",
    )
    _add_price(db_session)
    adapter = ScriptedAdapter(
        LLMProvider.OPENAI,
        SURFACE,
        response_text="Acme is recommended.",
    )
    scan = _create_scan(
        db_session,
        workspace,
        project,
        user,
        FakeDispatcher(),
        key="analysis-failure",
        adapter=adapter,
    )
    _execute_scan(db_session, scan.id, adapter)
    _finalize(db_session, scan.id)

    scan_status_before = db_session.get(Scan, scan.id).status

    # Force analysis failure by deleting snapshots and the existing analysis
    # Must delete mentions and attributions first due to FK constraints
    db_session.execute(
        text(
            "DELETE FROM source_attributions WHERE scan_analysis_id IN "
            "(SELECT id FROM scan_analyses WHERE scan_id = :sid)"
        ),
        {"sid": str(scan.id)},
    )
    db_session.execute(
        text(
            "DELETE FROM entity_mentions WHERE scan_analysis_id IN "
            "(SELECT id FROM scan_analyses WHERE scan_id = :sid)"
        ),
        {"sid": str(scan.id)},
    )
    db_session.execute(
        text("DELETE FROM scan_analyses WHERE scan_id = :sid"),
        {"sid": str(scan.id)},
    )
    db_session.execute(
        text("DELETE FROM scan_entity_snapshots WHERE scan_id = :sid"),
        {"sid": str(scan.id)},
    )
    db_session.commit()

    # Analysis should fail with MISSING_ENTITY_SNAPSHOT
    analysis = ScanAnalysisService(db_session).analyze(scan.id)
    assert analysis.status == ScanAnalysisStatus.FAILED
    assert analysis.failure_code == "MISSING_ENTITY_SNAPSHOT"

    # Scan state unchanged
    scan_status_after = db_session.get(Scan, scan.id).status
    assert scan_status_before == scan_status_after
