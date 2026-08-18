"""Integration tests for project onboarding service."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    FunnelStage,
    LLMProvider,
    ProjectStatus,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import QuotaExceededError, ValidationError
from app.models import (
    BillingAccount,
    PlanDefinition,
    PlanProvider,
    User,
    Workspace,
)
from app.models.workspace import WorkspaceMember
from app.services.project_onboarding_service import (
    CompetitorInput,
    KeywordInput,
    OnboardingRequest,
    ProjectOnboardingService,
)
from app.services.project_service import ProjectService, ProjectUpdateInput

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


def _setup_workspace_with_plan(
    db: Session,
    name: str = "Test WS",
    monthly_limit: int = 100,
    max_projects: int = 5,
    max_keywords: int = 20,
    max_competitors: int = 10,
    providers: list[LLMProvider] | None = None,
) -> tuple[Workspace, User]:
    user = User(email=f"p4-{uuid.uuid4().hex[:8]}@example.com", password_hash="h")
    ws = Workspace(name=name, workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, ws])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db.flush()

    plan = PlanDefinition(
        code=f"P4_{uuid.uuid4().hex[:8]}",
        name=f"P4 Plan {name}",
        is_active=True,
        max_projects=max_projects,
        max_keywords_per_project=max_keywords,
        max_competitors_per_project=max_competitors,
        max_team_members=5,
        monthly_ai_checks=monthly_limit,
    )
    db.add(plan)
    db.flush()
    for p in providers or [LLMProvider.OPENAI, LLMProvider.ANTHROPIC]:
        db.add(PlanProvider(plan_id=plan.id, provider=p))
    db.flush()
    db.add(
        BillingAccount(
            workspace_id=ws.id,
            source=BillingSource.ADMIN,
            status=BillingAccountStatus.ACTIVE,
            plan_code=plan.code,
            is_primary=True,
        )
    )
    db.commit()
    return ws, user


def _make_onboarding_request(
    name: str = "Acme Project",
    domain: str = "acme.com",
    brand_name: str = "Acme",
    keywords: list[KeywordInput] | None = None,
    competitors: list[CompetitorInput] | None = None,
    providers: list[LLMProvider] | None = None,
    target_language: str = "en",
    target_country: str = "US",
) -> OnboardingRequest:
    return OnboardingRequest(
        name=name,
        domain=domain,
        brand_name=brand_name,
        brand_aliases=["Acme Inc"],
        industry="SaaS",
        target_country=target_country,
        target_language=target_language,
        target_audience="Small Businesses",
        keywords=keywords
        if keywords is not None
        else [KeywordInput(text="best crm", funnel_stage=FunnelStage.PURCHASE)],
        competitors=competitors if competitors is not None else [],
        providers=providers if providers is not None else [LLMProvider.OPENAI],
    )


@pytest.fixture()
def engine_factory():
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True, pool_size=10)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield engine, factory
    engine.dispose()


@pytest.mark.integration
class TestProjectOnboarding:
    def test_successful_onboarding(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        svc = ProjectOnboardingService(db_session)
        request = _make_onboarding_request()

        project = svc.onboard_project(ws.id, request, created_by_user_id=user.id)

        assert project.id is not None
        assert project.name == "Acme Project"
        assert project.domain == "acme.com"
        assert project.status == ProjectStatus.ACTIVE
        assert project.prompt_input_revision == 1

        # Verify keyword was created.
        from app.repositories.keyword_repository import ProjectKeywordRepository

        kw_repo = ProjectKeywordRepository(db_session)
        keywords = kw_repo.list_by_project(project.id)
        assert len(keywords) == 1
        assert keywords[0].text == "best crm"
        assert keywords[0].normalized_text == "best crm"

        # Verify provider was created.
        from app.repositories.tracking_repository import ProjectProviderRepository

        pp_repo = ProjectProviderRepository(db_session)
        providers = pp_repo.list_by_project(project.id)
        assert len(providers) == 1
        assert providers[0].provider == LLMProvider.OPENAI

        # Verify prompt set was created.
        from app.repositories.tracking_repository import PromptSetRepository

        ps_repo = PromptSetRepository(db_session)
        active_set = ps_repo.get_active_by_project(project.id)
        assert active_set is not None
        assert active_set.version == 1
        assert active_set.input_revision == 1
        assert active_set.status == "ACTIVE"
        assert active_set.generator_key == "deterministic-template-v1"

        # Verify prompts were created (5 per keyword).
        from app.repositories.tracking_repository import PromptRepository

        prompt_repo = PromptRepository(db_session)
        prompt_count = prompt_repo.count_by_prompt_set(active_set.id)
        assert prompt_count == 5

    def test_onboarding_with_competitors(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        svc = ProjectOnboardingService(db_session)
        request = _make_onboarding_request(
            competitors=[CompetitorInput(name="Mailchimp", domain="mailchimp.com", aliases=["MCP"])]
        )

        project = svc.onboard_project(ws.id, request, created_by_user_id=user.id)

        from app.repositories.competitor_repository import CompetitorRepository

        comp_repo = CompetitorRepository(db_session)
        competitors = comp_repo.list_by_project(project.id)
        assert len(competitors) == 1
        assert competitors[0].name == "Mailchimp"
        assert competitors[0].domain == "mailchimp.com"

    def test_onboarding_normalizes_domain(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        svc = ProjectOnboardingService(db_session)
        request = _make_onboarding_request(domain="https://www.acme.com/page")

        project = svc.onboard_project(ws.id, request, created_by_user_id=user.id)
        assert project.domain == "acme.com"

    def test_onboarding_normalizes_keywords(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        svc = ProjectOnboardingService(db_session)
        request = _make_onboarding_request(keywords=[KeywordInput(text="  Best   CRM  ")])

        project = svc.onboard_project(ws.id, request, created_by_user_id=user.id)

        from app.repositories.keyword_repository import ProjectKeywordRepository

        kw_repo = ProjectKeywordRepository(db_session)
        keywords = kw_repo.list_by_project(project.id)
        assert len(keywords) == 1
        assert keywords[0].text == "Best CRM"
        assert keywords[0].normalized_text == "best crm"

    def test_onboarding_requires_at_least_one_keyword(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        svc = ProjectOnboardingService(db_session)
        request = _make_onboarding_request(keywords=[])

        with pytest.raises(ValidationError, match="keyword"):
            svc.onboard_project(ws.id, request, created_by_user_id=user.id)

    def test_onboarding_requires_at_least_one_provider(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        svc = ProjectOnboardingService(db_session)
        request = _make_onboarding_request(providers=[])

        with pytest.raises(ValidationError, match="provider"):
            svc.onboard_project(ws.id, request, created_by_user_id=user.id)

    def test_onboarding_rejects_disallowed_provider(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(
            db_session,
            providers=[LLMProvider.OPENAI],  # Only OPENAI allowed
        )
        svc = ProjectOnboardingService(db_session)
        request = _make_onboarding_request(providers=[LLMProvider.PERPLEXITY])

        from app.core.exceptions import EntitlementDeniedError

        with pytest.raises(EntitlementDeniedError):
            svc.onboard_project(ws.id, request, created_by_user_id=user.id)

    def test_onboarding_rejects_competitor_matching_project_domain(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        svc = ProjectOnboardingService(db_session)
        request = _make_onboarding_request(
            domain="acme.com",
            competitors=[CompetitorInput(name="Acme", domain="acme.com")],
        )

        with pytest.raises(ValidationError, match="cannot match"):
            svc.onboard_project(ws.id, request, created_by_user_id=user.id)

    def test_onboarding_project_limit_exceeded(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session, max_projects=1)
        svc = ProjectOnboardingService(db_session)

        # First project succeeds.
        svc.onboard_project(ws.id, _make_onboarding_request(), created_by_user_id=user.id)

        # Second project fails.
        with pytest.raises(QuotaExceededError, match="Project limit"):
            svc.onboard_project(
                ws.id,
                _make_onboarding_request(name="Second", domain="second.com"),
                created_by_user_id=user.id,
            )

    def test_onboarding_atomic_rollback_on_failure(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """If onboarding fails late, no partial project is left behind."""
        engine, factory = engine_factory

        # Setup workspace in its own session.
        setup = factory()
        try:
            ws, user = _setup_workspace_with_plan(
                setup,
                providers=[LLMProvider.OPENAI],  # Only OPENAI
            )
            ws_id = ws.id
            user_id = user.id
        finally:
            setup.close()

        # Onboarding in a separate session.
        session = factory()
        try:
            svc = ProjectOnboardingService(session)
            request = _make_onboarding_request(
                providers=[LLMProvider.OPENAI, LLMProvider.PERPLEXITY]
            )

            from app.core.exceptions import EntitlementDeniedError

            with pytest.raises(EntitlementDeniedError):
                svc.onboard_project(ws_id, request, created_by_user_id=user_id)
        finally:
            session.close()

        # Verify no project was created.
        verify = factory()
        try:
            from app.repositories.project_repository import ProjectRepository

            proj_repo = ProjectRepository(verify)
            projects = proj_repo.list_by_workspace(ws_id)
            assert len(projects) == 0
        finally:
            verify.close()

    def test_onboarding_portuguese(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        svc = ProjectOnboardingService(db_session)
        request = _make_onboarding_request(
            target_language="pt-BR",
            target_country="BR",
            brand_name="Acme",
        )

        project = svc.onboard_project(ws.id, request, created_by_user_id=user.id)
        assert project.target_language == "pt-br"

        # Verify prompts are in Portuguese.
        from app.repositories.tracking_repository import PromptRepository, PromptSetRepository

        ps_repo = PromptSetRepository(db_session)
        active_set = ps_repo.get_active_by_project(project.id)
        prompt_repo = PromptRepository(db_session)
        prompts = prompt_repo.list_by_prompt_set(active_set.id)
        assert any("melhores" in p.text.lower() for p in prompts)


@pytest.mark.integration
class TestProjectUpdate:
    def test_update_brand_name_increments_revision(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        onboarding = ProjectOnboardingService(db_session)
        project = onboarding.onboard_project(
            ws.id, _make_onboarding_request(), created_by_user_id=user.id
        )
        original_revision = project.prompt_input_revision

        svc = ProjectService(db_session)
        updated = svc.update_project(
            ws.id,
            project.id,
            ProjectUpdateInput(brand_name="NewBrand"),
        )
        assert updated.brand_name == "NewBrand"
        assert updated.prompt_input_revision == original_revision + 1

    def test_update_same_brand_name_no_revision_increment(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        onboarding = ProjectOnboardingService(db_session)
        project = onboarding.onboard_project(
            ws.id, _make_onboarding_request(brand_name="Acme"), created_by_user_id=user.id
        )
        original_revision = project.prompt_input_revision

        svc = ProjectService(db_session)
        updated = svc.update_project(
            ws.id,
            project.id,
            ProjectUpdateInput(brand_name="Acme"),  # Same name
        )
        assert updated.prompt_input_revision == original_revision

    def test_update_domain_increments_revision(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        onboarding = ProjectOnboardingService(db_session)
        project = onboarding.onboard_project(
            ws.id, _make_onboarding_request(), created_by_user_id=user.id
        )

        svc = ProjectService(db_session)
        updated = svc.update_project(
            ws.id,
            project.id,
            ProjectUpdateInput(domain="newdomain.com"),
        )
        assert updated.domain == "newdomain.com"
        assert updated.prompt_input_revision == 2

    def test_update_name_does_not_increment_revision(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        onboarding = ProjectOnboardingService(db_session)
        project = onboarding.onboard_project(
            ws.id, _make_onboarding_request(), created_by_user_id=user.id
        )
        original_revision = project.prompt_input_revision

        svc = ProjectService(db_session)
        updated = svc.update_project(
            ws.id,
            project.id,
            ProjectUpdateInput(name="New Name"),
        )
        assert updated.name == "New Name"
        assert updated.prompt_input_revision == original_revision


@pytest.mark.integration
class TestProjectStatus:
    def test_pause_and_activate(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        onboarding = ProjectOnboardingService(db_session)
        project = onboarding.onboard_project(
            ws.id, _make_onboarding_request(), created_by_user_id=user.id
        )

        svc = ProjectService(db_session)

        # Pause.
        paused = svc.pause_project(ws.id, project.id)
        assert paused.status == ProjectStatus.PAUSED

        # Activate.
        activated = svc.activate_project(ws.id, project.id)
        assert activated.status == ProjectStatus.ACTIVE

    def test_archive_frees_capacity(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session, max_projects=1)
        onboarding = ProjectOnboardingService(db_session)
        project = onboarding.onboard_project(
            ws.id, _make_onboarding_request(), created_by_user_id=user.id
        )

        svc = ProjectService(db_session)

        # Archive.
        archived = svc.archive_project(ws.id, project.id)
        assert archived.status == ProjectStatus.ARCHIVED

        # Can now create another project.
        project2 = onboarding.onboard_project(
            ws.id,
            _make_onboarding_request(name="Second", domain="second.com"),
            created_by_user_id=user.id,
        )
        assert project2.id is not None

    def test_reactivate_archived_rechecks_capacity(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session, max_projects=1)
        onboarding = ProjectOnboardingService(db_session)
        project = onboarding.onboard_project(
            ws.id, _make_onboarding_request(), created_by_user_id=user.id
        )

        svc = ProjectService(db_session)
        onboarding2 = ProjectOnboardingService(db_session)

        # Archive first.
        svc.archive_project(ws.id, project.id)

        # Create second project (fills the slot).
        onboarding2.onboard_project(
            ws.id,
            _make_onboarding_request(name="Second", domain="second.com"),
            created_by_user_id=user.id,
        )

        # Reactivating the archived project should fail (capacity exceeded).
        with pytest.raises(QuotaExceededError):
            svc.activate_project(ws.id, project.id)

    def test_pause_already_paused_is_noop(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        onboarding = ProjectOnboardingService(db_session)
        project = onboarding.onboard_project(
            ws.id, _make_onboarding_request(), created_by_user_id=user.id
        )

        svc = ProjectService(db_session)
        svc.pause_project(ws.id, project.id)
        # Pausing again should be a no-op (no error).
        result = svc.pause_project(ws.id, project.id)
        assert result.status == ProjectStatus.PAUSED


@pytest.mark.integration
class TestProjectSummary:
    def test_summary_with_prompt_set(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        onboarding = ProjectOnboardingService(db_session)
        project = onboarding.onboard_project(
            ws.id,
            _make_onboarding_request(
                keywords=[KeywordInput(text="best crm"), KeywordInput(text="email marketing")],
                competitors=[CompetitorInput(name="Mailchimp", domain="mailchimp.com")],
                providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
            ),
            created_by_user_id=user.id,
        )

        svc = ProjectService(db_session)
        summary = svc.get_project_summary(ws.id, project.id)

        assert summary.keyword_count == 2
        assert summary.competitor_count == 1
        assert summary.enabled_provider_count == 2
        assert summary.current_prompt_set_version == 1
        assert summary.is_prompt_set_stale is False
        # 10 prompts (2 keywords * 5) * 2 providers = 20
        assert summary.standard_scan_ai_checks_estimate == 20

    def test_summary_stale_after_update(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        onboarding = ProjectOnboardingService(db_session)
        project = onboarding.onboard_project(
            ws.id, _make_onboarding_request(), created_by_user_id=user.id
        )

        svc = ProjectService(db_session)
        # Update brand name → increments revision.
        svc.update_project(ws.id, project.id, ProjectUpdateInput(brand_name="NewBrand"))

        summary = svc.get_project_summary(ws.id, project.id)
        assert summary.is_prompt_set_stale is True
        assert summary.current_prompt_set_input_revision == 1
        assert summary.project_prompt_input_revision == 2
