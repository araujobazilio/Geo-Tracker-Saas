"""Integration tests for keyword and competitor management."""

from __future__ import annotations

import uuid

import pytest

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    FunnelStage,
    LLMProvider,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import ConflictError, QuotaExceededError, ValidationError
from app.models import (
    BillingAccount,
    PlanDefinition,
    PlanProvider,
    Project,
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
from app.services.tracking_service import (
    CompetitorCreateInput,
    CompetitorService,
    CompetitorUpdateInput,
    KeywordCreateInput,
    KeywordService,
    KeywordUpdateInput,
)


def _setup_workspace_with_plan(
    db,
    max_keywords: int = 20,
    max_competitors: int = 10,
    providers: list[LLMProvider] | None = None,
) -> tuple[Workspace, User, Project]:
    user = User(email=f"kw-{uuid.uuid4().hex[:8]}@example.com", password_hash="h")
    ws = Workspace(name="Test WS", workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, ws])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db.flush()

    plan = PlanDefinition(
        code=f"KW_{uuid.uuid4().hex[:8]}",
        name="KW Plan",
        is_active=True,
        max_projects=5,
        max_keywords_per_project=max_keywords,
        max_competitors_per_project=max_competitors,
        max_team_members=5,
        monthly_ai_checks=100,
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

    onboarding = ProjectOnboardingService(db)
    project = onboarding.onboard_project(
        ws.id,
        OnboardingRequest(
            name="Test Project",
            domain="acme.com",
            brand_name="Acme",
            target_country="US",
            target_language="en",
            keywords=[KeywordInput(text="best crm")],
            providers=[LLMProvider.OPENAI],
        ),
        created_by_user_id=user.id,
    )
    return ws, user, project


@pytest.mark.integration
class TestKeywordManagement:
    def test_add_keyword(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = KeywordService(db_session)

        keyword = svc.add_keyword(ws.id, project.id, KeywordCreateInput(text="email marketing"))
        assert keyword.id is not None
        assert keyword.text == "email marketing"
        assert keyword.normalized_text == "email marketing"
        assert keyword.active is True

    def test_add_keyword_normalizes(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = KeywordService(db_session)

        keyword = svc.add_keyword(
            ws.id, project.id, KeywordCreateInput(text="  Email   Marketing  ")
        )
        assert keyword.text == "Email Marketing"
        assert keyword.normalized_text == "email marketing"

    def test_add_duplicate_keyword_raises_conflict(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = KeywordService(db_session)

        # "best crm" already exists from onboarding.
        with pytest.raises(ConflictError, match="already exists"):
            svc.add_keyword(ws.id, project.id, KeywordCreateInput(text="Best CRM"))

    def test_add_keyword_increments_revision(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = KeywordService(db_session)
        original_revision = project.prompt_input_revision

        svc.add_keyword(ws.id, project.id, KeywordCreateInput(text="email marketing"))

        db_session.refresh(project)
        assert project.prompt_input_revision == original_revision + 1

    def test_update_keyword_intent(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = KeywordService(db_session)

        from app.repositories.keyword_repository import ProjectKeywordRepository

        kw_repo = ProjectKeywordRepository(db_session)
        keywords = kw_repo.list_by_project(project.id)
        keyword_id = keywords[0].id

        updated = svc.update_keyword(
            ws.id,
            project.id,
            keyword_id,
            KeywordUpdateInput(intent="researching"),
        )
        assert updated.intent == "researching"

    def test_update_keyword_funnel_stage_increments_revision(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = KeywordService(db_session)
        original_revision = project.prompt_input_revision

        from app.repositories.keyword_repository import ProjectKeywordRepository

        kw_repo = ProjectKeywordRepository(db_session)
        keyword_id = kw_repo.list_by_project(project.id)[0].id

        svc.update_keyword(
            ws.id,
            project.id,
            keyword_id,
            KeywordUpdateInput(funnel_stage=FunnelStage.AWARENESS),
        )

        db_session.refresh(project)
        assert project.prompt_input_revision == original_revision + 1

    def test_deactivate_keyword(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = KeywordService(db_session)

        from app.repositories.keyword_repository import ProjectKeywordRepository

        kw_repo = ProjectKeywordRepository(db_session)
        keyword_id = kw_repo.list_by_project(project.id)[0].id

        updated = svc.update_keyword(
            ws.id, project.id, keyword_id, KeywordUpdateInput(active=False)
        )
        assert updated.active is False

    def test_keyword_limit_enforced(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session, max_keywords=2)
        svc = KeywordService(db_session)

        # Project already has 1 keyword from onboarding.
        svc.add_keyword(ws.id, project.id, KeywordCreateInput(text="second kw"))

        # Third keyword should fail.
        with pytest.raises(QuotaExceededError, match="Keyword limit"):
            svc.add_keyword(ws.id, project.id, KeywordCreateInput(text="third kw"))

    def test_inactive_keyword_frees_capacity(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session, max_keywords=2)
        svc = KeywordService(db_session)

        from app.repositories.keyword_repository import ProjectKeywordRepository

        kw_repo = ProjectKeywordRepository(db_session)
        keyword_id = kw_repo.list_by_project(project.id)[0].id

        # Deactivate the first keyword (0 active now).
        svc.update_keyword(ws.id, project.id, keyword_id, KeywordUpdateInput(active=False))

        # Can add 2 new (capacity = 2).
        svc.add_keyword(ws.id, project.id, KeywordCreateInput(text="second kw"))
        svc.add_keyword(ws.id, project.id, KeywordCreateInput(text="third kw"))
        # Fourth should fail (would be 3 active > 2 max).
        with pytest.raises(QuotaExceededError):
            svc.add_keyword(ws.id, project.id, KeywordCreateInput(text="fourth kw"))

    def test_reactivate_keyword_enforces_capacity(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session, max_keywords=2)
        svc = KeywordService(db_session)

        from app.repositories.keyword_repository import ProjectKeywordRepository

        kw_repo = ProjectKeywordRepository(db_session)
        first_kw_id = kw_repo.list_by_project(project.id)[0].id

        # Deactivate first, add second and third (fills capacity = 2).
        svc.update_keyword(ws.id, project.id, first_kw_id, KeywordUpdateInput(active=False))
        svc.add_keyword(ws.id, project.id, KeywordCreateInput(text="second kw"))
        svc.add_keyword(ws.id, project.id, KeywordCreateInput(text="third kw"))

        # Reactivating first should fail (would be 3 active > 2 max).
        with pytest.raises(QuotaExceededError):
            svc.update_keyword(ws.id, project.id, first_kw_id, KeywordUpdateInput(active=True))

    def test_cross_project_keyword_idor(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Keyword from project B cannot be accessed via project A."""
        ws, user, project_a = _setup_workspace_with_plan(db_session)

        # Create project B.
        onboarding = ProjectOnboardingService(db_session)
        project_b = onboarding.onboard_project(
            ws.id,
            OnboardingRequest(
                name="Project B",
                domain="projb.com",
                brand_name="ProjB",
                target_language="en",
                keywords=[KeywordInput(text="different kw")],
                providers=[LLMProvider.OPENAI],
            ),
            created_by_user_id=user.id,
        )

        from app.repositories.keyword_repository import ProjectKeywordRepository

        kw_repo = ProjectKeywordRepository(db_session)
        kw_b_id = kw_repo.list_by_project(project_b.id)[0].id

        svc = KeywordService(db_session)
        # Try to update project B's keyword via project A.
        with pytest.raises(ConflictError, match="not found"):
            svc.update_keyword(
                ws.id,
                project_a.id,
                kw_b_id,
                KeywordUpdateInput(intent="hacked"),
            )

    def test_bulk_keyword_addition(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session, max_keywords=10)
        svc = KeywordService(db_session)

        keywords = svc.add_keywords_bulk(
            ws.id,
            project.id,
            [
                KeywordCreateInput(text="email marketing"),
                KeywordCreateInput(text="crm software"),
                KeywordCreateInput(text="best saas"),
            ],
        )
        assert len(keywords) == 3

    def test_bulk_keyword_limit_enforced(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session, max_keywords=3)
        svc = KeywordService(db_session)

        # Project already has 1 keyword. Try to add 3 more (total 4 > 3).
        with pytest.raises(QuotaExceededError):
            svc.add_keywords_bulk(
                ws.id,
                project.id,
                [
                    KeywordCreateInput(text="kw1"),
                    KeywordCreateInput(text="kw2"),
                    KeywordCreateInput(text="kw3"),
                ],
            )


@pytest.mark.integration
class TestCompetitorManagement:
    def test_add_competitor(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = CompetitorService(db_session)

        comp = svc.add_competitor(
            ws.id,
            project.id,
            CompetitorCreateInput(name="Mailchimp", domain="mailchimp.com"),
        )
        assert comp.id is not None
        assert comp.domain == "mailchimp.com"

    def test_add_competitor_normalizes_domain(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = CompetitorService(db_session)

        comp = svc.add_competitor(
            ws.id,
            project.id,
            CompetitorCreateInput(name="HubSpot", domain="https://www.hubspot.com/page"),
        )
        assert comp.domain == "hubspot.com"

    def test_add_duplicate_competitor_domain_raises(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = CompetitorService(db_session)

        svc.add_competitor(
            ws.id, project.id, CompetitorCreateInput(name="Mailchimp", domain="mailchimp.com")
        )

        with pytest.raises(ConflictError, match="already exists"):
            svc.add_competitor(
                ws.id,
                project.id,
                CompetitorCreateInput(name="Mailchimp2", domain="https://www.mailchimp.com/"),
            )

    def test_add_competitor_matching_project_domain_raises(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = CompetitorService(db_session)

        with pytest.raises(ValidationError, match="cannot match"):
            svc.add_competitor(
                ws.id,
                project.id,
                CompetitorCreateInput(name="Acme", domain="acme.com"),
            )

    def test_add_competitor_increments_revision(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = CompetitorService(db_session)
        original_revision = project.prompt_input_revision

        svc.add_competitor(
            ws.id, project.id, CompetitorCreateInput(name="Mailchimp", domain="mailchimp.com")
        )

        db_session.refresh(project)
        assert project.prompt_input_revision == original_revision + 1

    def test_competitor_limit_enforced(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session, max_competitors=1)
        svc = CompetitorService(db_session)

        svc.add_competitor(
            ws.id, project.id, CompetitorCreateInput(name="Mailchimp", domain="mailchimp.com")
        )

        with pytest.raises(QuotaExceededError, match="Competitor limit"):
            svc.add_competitor(
                ws.id,
                project.id,
                CompetitorCreateInput(name="HubSpot", domain="hubspot.com"),
            )

    def test_deactivate_competitor_frees_capacity(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user, project = _setup_workspace_with_plan(db_session, max_competitors=1)
        svc = CompetitorService(db_session)

        comp = svc.add_competitor(
            ws.id, project.id, CompetitorCreateInput(name="Mailchimp", domain="mailchimp.com")
        )

        # Deactivate.
        svc.update_competitor(ws.id, project.id, comp.id, CompetitorUpdateInput(active=False))

        # Can now add another.
        comp2 = svc.add_competitor(
            ws.id, project.id, CompetitorCreateInput(name="HubSpot", domain="hubspot.com")
        )
        assert comp2.id is not None

    def test_cross_project_competitor_idor(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Competitor from project B cannot be accessed via project A."""
        ws, user, project_a = _setup_workspace_with_plan(db_session)

        onboarding = ProjectOnboardingService(db_session)
        project_b = onboarding.onboard_project(
            ws.id,
            OnboardingRequest(
                name="Project B",
                domain="projb.com",
                brand_name="ProjB",
                target_language="en",
                keywords=[KeywordInput(text="different kw")],
                competitors=[CompetitorInput(name="CompB", domain="compb.com")],
                providers=[LLMProvider.OPENAI],
            ),
            created_by_user_id=user.id,
        )

        from app.repositories.competitor_repository import CompetitorRepository

        comp_repo = CompetitorRepository(db_session)
        comp_b_id = comp_repo.list_by_project(project_b.id)[0].id

        svc = CompetitorService(db_session)
        with pytest.raises(ConflictError, match="not found"):
            svc.update_competitor(
                ws.id,
                project_a.id,
                comp_b_id,
                CompetitorUpdateInput(name="Hacked"),
            )

    def test_competitor_domain_immutable(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """Competitor domain cannot be changed via update (no domain field in update)."""
        ws, user, project = _setup_workspace_with_plan(db_session)
        svc = CompetitorService(db_session)

        comp = svc.add_competitor(
            ws.id, project.id, CompetitorCreateInput(name="Mailchimp", domain="mailchimp.com")
        )

        # Update name only — domain remains unchanged.
        updated = svc.update_competitor(
            ws.id, project.id, comp.id, CompetitorUpdateInput(name="Mailchimp International")
        )
        assert updated.name == "Mailchimp International"
        assert updated.domain == "mailchimp.com"  # Unchanged
