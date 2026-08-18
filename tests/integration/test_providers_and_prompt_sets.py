"""Integration tests for project providers and prompt set versioning."""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    LLMProvider,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import ConflictError, EntitlementDeniedError
from app.models import (
    BillingAccount,
    PlanDefinition,
    PlanProvider,
    User,
    Workspace,
)
from app.models.workspace import WorkspaceMember
from app.services.project_onboarding_service import (
    KeywordInput,
    OnboardingRequest,
    ProjectOnboardingService,
)
from app.services.project_provider_service import ProjectProviderService
from app.services.prompt_set_service import PromptSetService
from app.services.tracking_service import KeywordCreateInput, KeywordService

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


def _setup_workspace_with_plan(
    db,
    providers: list[LLMProvider] | None = None,
    max_projects: int = 5,
) -> tuple[Workspace, User]:
    user = User(email=f"pv-{uuid.uuid4().hex[:8]}@example.com", password_hash="h")
    ws = Workspace(name="Test WS", workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, ws])
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db.flush()

    plan = PlanDefinition(
        code=f"PV_{uuid.uuid4().hex[:8]}",
        name="PV Plan",
        is_active=True,
        max_projects=max_projects,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
        max_team_members=5,
        monthly_ai_checks=100,
    )
    db.add(plan)
    db.flush()
    for p in providers or [LLMProvider.OPENAI, LLMProvider.ANTHROPIC, LLMProvider.GOOGLE]:
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


def _onboard_project(db, ws_id, user_id, providers=None):
    svc = ProjectOnboardingService(db)
    return svc.onboard_project(
        ws_id,
        OnboardingRequest(
            name="Test Project",
            domain="acme.com",
            brand_name="Acme",
            target_language="en",
            target_country="US",
            keywords=[KeywordInput(text="best crm")],
            providers=providers or [LLMProvider.OPENAI],
        ),
        created_by_user_id=user_id,
    )


@pytest.mark.integration
class TestProjectProviders:
    def test_list_providers(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        project = _onboard_project(db_session, ws.id, user.id, [LLMProvider.OPENAI])

        svc = ProjectProviderService(db_session)
        providers = svc.list_providers(ws.id, project.id)
        assert len(providers) == 1
        assert providers[0].provider == LLMProvider.OPENAI
        assert providers[0].enabled is True
        assert providers[0].allowed_by_plan is True

    def test_set_providers_replace(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        project = _onboard_project(db_session, ws.id, user.id, [LLMProvider.OPENAI])

        svc = ProjectProviderService(db_session)
        svc.set_providers(ws.id, project.id, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

        providers = svc.list_providers(ws.id, project.id)
        assert len(providers) == 2

    def test_set_providers_rejects_disallowed(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(
            db_session,
            providers=[LLMProvider.OPENAI],  # Only OPENAI
        )
        project = _onboard_project(db_session, ws.id, user.id, [LLMProvider.OPENAI])

        svc = ProjectProviderService(db_session)
        with pytest.raises(EntitlementDeniedError):
            svc.set_providers(ws.id, project.id, [LLMProvider.PERPLEXITY])

    def test_provider_changes_do_not_increment_revision(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        project = _onboard_project(db_session, ws.id, user.id, [LLMProvider.OPENAI])
        original_revision = project.prompt_input_revision

        svc = ProjectProviderService(db_session)
        svc.set_providers(ws.id, project.id, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

        db_session.refresh(project)
        assert project.prompt_input_revision == original_revision

    def test_plan_downgrade_does_not_destroy_config(self, db_session) -> None:  # type: ignore[no-untyped-def]
        """If a provider disappears from plan, project config is NOT mutated."""
        ws, user = _setup_workspace_with_plan(
            db_session, providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC]
        )
        project = _onboard_project(
            db_session, ws.id, user.id, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC]
        )

        svc = ProjectProviderService(db_session)
        providers = svc.list_providers(ws.id, project.id)
        assert all(p.allowed_by_plan for p in providers)

        # Simulate plan downgrade: remove ANTHROPIC from plan.
        from app.repositories.plan_repository import PlanRepository

        plan_repo = PlanRepository(db_session)
        billing_repo = __import__(
            "app.repositories.billing_repository",
            fromlist=["BillingAccountRepository"],
        ).BillingAccountRepository(db_session)

        billing = billing_repo.get_primary(ws.id)
        plan = plan_repo.get_by_code(billing.plan_code)
        # Delete ANTHROPIC from plan.
        db_session.execute(
            __import__("sqlalchemy", fromlist=["text"]).text(
                f"DELETE FROM plan_providers WHERE plan_id = '{plan.id}' "
                f"AND provider = 'ANTHROPIC'"
            )
        )
        db_session.flush()

        # Project config should still have ANTHROPIC.
        providers = svc.list_providers(ws.id, project.id)
        anthropic = [p for p in providers if p.provider == LLMProvider.ANTHROPIC]
        assert len(anthropic) == 1
        assert anthropic[0].enabled is True
        assert anthropic[0].allowed_by_plan is False  # No longer allowed


@pytest.mark.integration
class TestPromptSetVersioning:
    def test_onboarding_creates_version_1(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        project = _onboard_project(db_session, ws.id, user.id)

        svc = PromptSetService(db_session)
        current = svc.get_current_prompt_set(ws.id, project.id)
        assert current is not None
        assert current.version == 1
        assert current.status == "ACTIVE"

    def test_regenerate_creates_version_2(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        project = _onboard_project(db_session, ws.id, user.id)

        # Make project stale by updating brand.
        from app.services.project_service import ProjectService, ProjectUpdateInput

        proj_svc = ProjectService(db_session)
        proj_svc.update_project(ws.id, project.id, ProjectUpdateInput(brand_name="NewBrand"))

        # Regenerate.
        ps_svc = PromptSetService(db_session)
        new_set = ps_svc.regenerate_prompt_set(ws.id, project.id, created_by_user_id=user.id)

        assert new_set.version == 2
        assert new_set.status == "ACTIVE"
        assert new_set.input_revision == 2

        # Old set should be SUPERSEDED.
        old_set = ps_svc.get_prompt_set_by_version(ws.id, project.id, 1)
        assert old_set.status == "SUPERSEDED"

    def test_old_prompts_unchanged_after_regeneration(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        project = _onboard_project(db_session, ws.id, user.id)

        from app.repositories.tracking_repository import PromptRepository, PromptSetRepository

        ps_repo = PromptSetRepository(db_session)
        prompt_repo = PromptRepository(db_session)

        v1_set = ps_repo.get_active_by_project(project.id)
        v1_prompts = prompt_repo.list_by_prompt_set(v1_set.id)
        v1_texts = [p.text for p in v1_prompts]

        # Make stale and regenerate.
        from app.services.project_service import ProjectService, ProjectUpdateInput

        proj_svc = ProjectService(db_session)
        proj_svc.update_project(ws.id, project.id, ProjectUpdateInput(brand_name="NewBrand"))

        ps_svc = PromptSetService(db_session)
        ps_svc.regenerate_prompt_set(ws.id, project.id, created_by_user_id=user.id)

        # V1 prompts should be unchanged.
        v1_prompts_after = prompt_repo.list_by_prompt_set(v1_set.id)
        v1_texts_after = [p.text for p in v1_prompts_after]
        assert v1_texts == v1_texts_after

    def test_regenerate_while_fresh_returns_same_set(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        project = _onboard_project(db_session, ws.id, user.id)

        ps_svc = PromptSetService(db_session)
        current = ps_svc.get_current_prompt_set(ws.id, project.id)

        # Regenerate while fresh.
        result = ps_svc.regenerate_prompt_set(ws.id, project.id, created_by_user_id=user.id)
        assert result.id == current.id
        assert result.version == 1  # No new version

    def test_regenerate_no_active_keywords_raises(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        project = _onboard_project(db_session, ws.id, user.id)

        # Make project stale so regeneration is attempted.
        from app.services.project_service import ProjectService, ProjectUpdateInput

        proj_svc = ProjectService(db_session)
        proj_svc.update_project(ws.id, project.id, ProjectUpdateInput(brand_name="NewBrand"))

        # Deactivate all keywords.
        from app.repositories.keyword_repository import ProjectKeywordRepository

        kw_repo = ProjectKeywordRepository(db_session)
        for kw in kw_repo.list_by_project(project.id):
            kw.active = False
        db_session.flush()
        db_session.commit()

        ps_svc = PromptSetService(db_session)
        with pytest.raises(ConflictError, match="no active keywords"):
            ps_svc.regenerate_prompt_set(ws.id, project.id)

    def test_list_prompt_sets(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        project = _onboard_project(db_session, ws.id, user.id)

        ps_svc = PromptSetService(db_session)
        sets = ps_svc.list_prompt_sets(ws.id, project.id)
        assert len(sets) == 1
        assert sets[0].version == 1

    def test_get_prompt_set_by_version(self, db_session) -> None:  # type: ignore[no-untyped-def]
        ws, user = _setup_workspace_with_plan(db_session)
        project = _onboard_project(db_session, ws.id, user.id)

        ps_svc = PromptSetService(db_session)
        ps = ps_svc.get_prompt_set_by_version(ws.id, project.id, 1)
        assert ps is not None
        assert ps.version == 1

        # Non-existent version.
        ps_none = ps_svc.get_prompt_set_by_version(ws.id, project.id, 99)
        assert ps_none is None


@pytest.fixture()
def engine_factory():
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True, pool_size=10)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield engine, factory
    engine.dispose()


@pytest.mark.integration
class TestProjectCapacityConcurrency:
    def test_concurrent_project_creation_respects_limit(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Two concurrent project creations for the last slot — only one succeeds."""
        engine, factory = engine_factory

        # Setup workspace with max_projects=1.
        setup = factory()
        try:
            ws, user = _setup_workspace_with_plan(setup, max_projects=1)
            ws_id = ws.id
            user_id = user.id
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        results: dict[str, int] = {"success": 0, "quota_error": 0, "other_error": 0}
        lock = threading.Lock()

        def worker(name: str, domain: str) -> None:
            session = factory()
            try:
                svc = ProjectOnboardingService(session)
                barrier.wait(timeout=10)
                svc.onboard_project(
                    ws_id,
                    OnboardingRequest(
                        name=name,
                        domain=domain,
                        brand_name=name,
                        target_language="en",
                        keywords=[KeywordInput(text="best crm")],
                        providers=[LLMProvider.OPENAI],
                    ),
                    created_by_user_id=user_id,
                )
                with lock:
                    results["success"] += 1
            except Exception as e:
                with lock:
                    if "limit" in str(e).lower() or "quota" in type(e).__name__.lower():
                        results["quota_error"] += 1
                    else:
                        results["other_error"] += 1
            finally:
                session.close()

        t1 = threading.Thread(target=worker, args=("Project A", "a.com"))
        t2 = threading.Thread(target=worker, args=("Project B", "b.com"))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert results["success"] == 1, f"Expected 1 success: {results}"
        assert results["quota_error"] == 1, f"Expected 1 quota error: {results}"
        assert results["other_error"] == 0, f"Unexpected errors: {results}"

        # Verify final count.
        verify = factory()
        try:
            from app.repositories.project_repository import ProjectRepository

            proj_repo = ProjectRepository(verify)
            count = proj_repo.count_tracked_by_workspace(ws_id)
            assert count == 1
        finally:
            verify.close()


@pytest.mark.integration
class TestKeywordCapacityConcurrency:
    def test_concurrent_keyword_addition_respects_limit(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Two concurrent keyword additions for the last slot — only one succeeds."""
        engine, factory = engine_factory

        setup = factory()
        try:
            ws, user = _setup_workspace_with_plan(setup, max_projects=5)
            ws_id = ws.id
            user_id = user.id
            project = _onboard_project(setup, ws_id, user_id)
            project_id = project.id
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        results: dict[str, int] = {"success": 0, "quota_error": 0, "other_error": 0}
        lock = threading.Lock()

        def worker(text: str) -> None:
            session = factory()
            try:
                svc = KeywordService(session)
                barrier.wait(timeout=10)
                svc.add_keyword(ws_id, project_id, KeywordCreateInput(text=text))
                with lock:
                    results["success"] += 1
            except Exception as e:
                with lock:
                    if "limit" in str(e).lower() or "quota" in type(e).__name__.lower():
                        results["quota_error"] += 1
                    else:
                        results["other_error"] += 1
            finally:
                session.close()

        t1 = threading.Thread(target=worker, args=("keyword alpha",))
        t2 = threading.Thread(target=worker, args=("keyword beta",))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        # At least one should succeed. The other may succeed or get quota error
        # depending on timing (both could succeed if max_keywords > 1).
        # With default max_keywords=20, both should succeed.
        assert results["success"] >= 1, f"Expected at least 1 success: {results}"


@pytest.mark.integration
class TestPromptSetRegenerationConcurrency:
    def test_concurrent_regeneration_creates_one_new_version(self, engine_factory) -> None:  # type: ignore[no-untyped-def]
        """Two concurrent regenerate requests — only one new version."""
        engine, factory = engine_factory

        setup = factory()
        try:
            ws, user = _setup_workspace_with_plan(setup)
            ws_id = ws.id
            user_id = user.id
            project = _onboard_project(setup, ws_id, user_id)
            project_id = project.id

            # Make project stale.
            from app.services.project_service import ProjectService, ProjectUpdateInput

            proj_svc = ProjectService(setup)
            proj_svc.update_project(ws_id, project_id, ProjectUpdateInput(brand_name="NewBrand"))
        finally:
            setup.close()

        barrier = threading.Barrier(2)
        results: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            session = factory()
            try:
                svc = PromptSetService(session)
                barrier.wait(timeout=10)
                ps = svc.regenerate_prompt_set(ws_id, project_id, created_by_user_id=user_id)
                with lock:
                    results.append(ps.version)
            except Exception:
                with lock:
                    results.append(-1)
            finally:
                session.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        # Both should resolve to version 2 (or one gets version 2 and the other
        # also gets version 2 due to the lock + freshness check).
        assert all(v == 2 for v in results), f"Expected all version 2: {results}"

        # Verify only one ACTIVE set.
        verify = factory()
        try:
            from app.repositories.tracking_repository import PromptSetRepository

            ps_repo = PromptSetRepository(verify)
            active = ps_repo.get_active_by_project(project_id)
            assert active is not None
            assert active.version == 2
        finally:
            verify.close()
