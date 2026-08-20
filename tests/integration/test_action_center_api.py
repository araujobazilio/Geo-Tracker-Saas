"""Phase 9 Action Center and Competitor Explanation API integration tests.

Tests cover:
- Competitor explanation API endpoints (list, detail)
- Opportunity API endpoints (list, detail, status update)
- Action refresh API endpoint
- Tenant isolation (cross-workspace → 403)
- Role matrix (OWNER/ADMIN: read + refresh + update; MEMBER: read only)
- Zero-cost verification via API
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    LLMProvider,
    ProjectStatus,
    PromptSetStatus,
    PromptType,
    ProviderSurface,
    ScanType,
    WorkspaceRole,
)
from app.db.redis import get_redis, reset_redis
from app.db.session import reset_engine
from app.main import create_app
from app.models import (
    BillingAccount,
    Competitor,
    Opportunity,
    PlanDefinition,
    PlanProvider,
    Project,
    ProjectKeyword,
    ProjectProvider,
    Prompt,
    PromptSet,
    ProviderPriceRule,
    ScanEntitySnapshot,
    WorkspaceMember,
)
from app.services.action_generation_service import ActionGenerationService
from app.services.prompt_generation_service import GENERATOR_KEY
from app.services.scan_analysis_service import ScanAnalysisService
from app.services.scan_creation_service import ScanCreationService
from app.services.scan_execution_service import ScanExecutionService
from app.services.scan_finalization_service import ScanFinalizationService

pytestmark = pytest.mark.integration

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)

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


# ----------------------------------------------------------------------
# Fake adapter
# ----------------------------------------------------------------------


class _ScriptedAdapter:
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
    ) -> None:
        self.provider = provider
        self.surface = surface
        self.brand_name = brand_name
        self.competitor_name = competitor_name
        self.mention_mode = mention_mode
        self.brand_citation_url = brand_citation_url
        self.competitor_citation_url = competitor_citation_url
        self.requests: list[object] = []

    def capabilities(self):
        from app.providers.base import ProviderCapabilities

        return ProviderCapabilities(
            supports_model_only=True,
            supports_web_grounded=True,
            supports_citations=True,
            supports_search_result_metadata=True,
        )

    async def execute(self, request):
        from app.providers.base import (
            ProviderCitation,
            ProviderResult,
            ProviderUsage,
        )
        from app.providers.base import (
            ProviderExecutionMode as ExecMode,
        )

        self.requests.append(request)
        parts: list[str] = []
        if self.mention_mode in ("brand", "both"):
            parts.append(f"{self.brand_name} is a great choice.")
        if self.mention_mode in ("competitor", "both"):
            parts.append(f"{self.competitor_name} is also worth considering.")
        if not parts:
            parts.append("Here is a generic answer with no tracked entities.")
        text = " ".join(parts)

        citations: list[ProviderCitation] = []
        if request.mode == ExecMode.WEB_GROUNDED:
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
                search_requests=1 if request.mode == ExecMode.WEB_GROUNDED else 0,
            ),
            provider_request_id=f"request-{len(self.requests)}",
            provider_response_id=f"response-{len(self.requests)}",
            finish_reason="stop",
            latency_ms=7,
            search_used=request.mode == ExecMode.WEB_GROUNDED,
        )


class _ScriptedRegistry:
    def __init__(self, adapters: dict[LLMProvider, _ScriptedAdapter]) -> None:
        self.adapters = adapters

    def get(self, provider: LLMProvider) -> _ScriptedAdapter:
        return self.adapters[provider]


class _FakeDispatcher:
    def __init__(self) -> None:
        self.scan_ids: list[uuid.UUID] = []

    def dispatch(self, scan_id: uuid.UUID) -> None:
        self.scan_ids.append(scan_id)


# ----------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------


@contextmanager
def _db_session() -> Iterator[Session]:
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


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


def _settings(models: dict[LLMProvider, str] | None = None):
    from app.config import Settings

    models = models or MODELS
    return Settings(
        app_env="test",
        openai_api_key="synthetic-openai-key",
        openai_scan_model=models.get(LLMProvider.OPENAI, MODELS[LLMProvider.OPENAI]),
        anthropic_api_key="synthetic-anthropic-key",
        anthropic_scan_model=models.get(LLMProvider.ANTHROPIC, MODELS[LLMProvider.ANTHROPIC]),
        google_api_key="synthetic-google-key",
        google_scan_model=models.get(LLMProvider.GOOGLE, MODELS[LLMProvider.GOOGLE]),
        perplexity_api_key="synthetic-perplexity-key",
        perplexity_scan_model=models.get(LLMProvider.PERPLEXITY, MODELS[LLMProvider.PERPLEXITY]),
        pricing_require_rule_for_execution=True,
        scan_max_concurrency=2,
        scan_stale_after_seconds=60,
    )


def _add_prices(
    db: Session, providers: list[LLMProvider], models: dict[LLMProvider, str] | None = None
) -> None:
    models = models or MODELS
    now = datetime.now(UTC)
    db.add_all(
        [
            ProviderPriceRule(
                pricing_key=f"synthetic:{provider.value}:{uuid.uuid4().hex}",
                provider=provider,
                provider_surface=SURFACES[provider],
                model=models[provider],
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


def _seed_full_pipeline(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    providers: list[LLMProvider] | None = None,
    prompt_count: int = 5,
    competitors: list[tuple[str, str]] | None = None,
    brand_name: str = "Acme",
    brand_domain: str = "acme.test",
    mention_mode: str = "competitor",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed plan, project, prompts, run full scan pipeline.

    Returns (project_id, scan_id, comp_snapshot_id).
    """
    providers = providers or [LLMProvider.OPENAI]
    comp_name = competitors[0][0] if competitors else "Rival"
    comp_domain = competitors[0][1] if competitors else "rival.test"
    suffix = uuid.uuid4().hex
    # Use unique model names per call to avoid price rule conflicts in CI.
    models = {p: f"synthetic-{p.value.lower()}-{suffix}" for p in providers}

    # Seed prerequisites.
    with _db_session() as db:
        plan = PlanDefinition(
            code=f"P9API_{suffix}",
            name="Synthetic phase9 API plan",
            is_active=True,
            max_projects=10,
            max_keywords_per_project=20,
            max_competitors_per_project=10,
            max_team_members=5,
            monthly_ai_checks=1000,
            confidence_scans_enabled=True,
        )
        db.add(plan)
        db.flush()
        db.add_all([PlanProvider(plan_id=plan.id, provider=p) for p in providers])
        db.add(
            BillingAccount(
                workspace_id=workspace_id,
                source=BillingSource.ADMIN,
                status=BillingAccountStatus.ACTIVE,
                plan_code=plan.code,
                is_primary=True,
            )
        )

        project = Project(
            workspace_id=workspace_id,
            name="Synthetic API project",
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
            [ProjectProvider(project_id=project.id, provider=p, enabled=True) for p in providers]
        )
        keyword = ProjectKeyword(
            project_id=project.id,
            text="synthetic query",
            normalized_text="synthetic query",
            intent="research",
            active=True,
        )
        db.add(keyword)
        db.flush()

        for cn, cd in competitors or []:
            db.add(
                Competitor(
                    project_id=project.id,
                    name=cn,
                    domain=cd,
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
            created_by_user_id=user_id,
            activated_at=datetime.now(UTC),
        )
        db.add(prompt_set)
        db.flush()
        prompts = [
            Prompt(
                prompt_set_id=prompt_set.id,
                project_keyword_id=keyword.id,
                variant_index=i,
                text=f"best CRM for small business {i}",
                prompt_type=PromptType.NON_BRANDED,
                intent="research",
                target_country="US",
                target_language="en",
                active=True,
            )
            for i in range(1, prompt_count + 1)
        ]
        db.add_all(prompts)
        _add_prices(db, providers, models)
        db.commit()

    # Create scan.
    with _db_session() as db:
        registry = _ScriptedRegistry(
            {
                p: _ScriptedAdapter(
                    p,
                    SURFACES[p],
                    brand_name=brand_name,
                    competitor_name=comp_name,
                    mention_mode=mention_mode,
                    brand_citation_url=f"https://{brand_domain}/page" if brand_domain else None,
                    competitor_citation_url=f"https://{comp_domain}/page" if comp_domain else None,
                )
                for p in providers
            }
        )
        dispatcher = _FakeDispatcher()
        result = ScanCreationService(
            db,
            dispatcher,
            settings=_settings(models),
            registry=registry,  # type: ignore[arg-type]
        ).create_scan(
            workspace_id,
            project.id,
            ScanType.STANDARD,
            user_id,
            f"api-scan-{suffix}",
        )
        scan_id = result.scan.id
        db.commit()

    # Execute scan.
    with _db_session() as db:
        registry = _ScriptedRegistry(
            {
                p: _ScriptedAdapter(
                    p,
                    SURFACES[p],
                    brand_name=brand_name,
                    competitor_name=comp_name,
                    mention_mode=mention_mode,
                    brand_citation_url=f"https://{brand_domain}/page" if brand_domain else None,
                    competitor_citation_url=f"https://{comp_domain}/page" if comp_domain else None,
                )
                for p in providers
            }
        )
        factory = sessionmaker(
            bind=db.get_bind(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        asyncio.run(
            ScanExecutionService(
                factory,
                registry=registry,  # type: ignore[arg-type]
                settings=_settings(models),
            ).execute_scan(scan_id)
        )
        db.commit()

    # Finalize + analyze.
    with _db_session() as db:
        ScanFinalizationService(db, analysis_session_factory=_connection_factory(db)).finalize(
            scan_id, trigger_analysis=True
        )
        ScanAnalysisService(db, failure_session_factory=_connection_factory(db)).analyze(scan_id)
        db.commit()

    # Get competitor snapshot ID.
    with _db_session() as db:
        snaps = list(
            db.execute(
                select(ScanEntitySnapshot).where(ScanEntitySnapshot.scan_id == scan_id)
            ).scalars()
        )
        comp_snap = next(
            s
            for s in snaps
            if (s.entity_type.value if hasattr(s.entity_type, "value") else s.entity_type)
            == "COMPETITOR"
        )
        comp_snap_id = comp_snap.id

    return project.id, scan_id, comp_snap_id


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture()
def api(
    prepared_test_db: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, FastAPI]]:
    assert prepared_test_db == TEST_DATABASE_URL
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-never-sent")
    monkeypatch.setenv("OPENAI_SCAN_MODEL", "scan-api-test-model")
    monkeypatch.setenv("PRICING_REQUIRE_RULE_FOR_EXECUTION", "false")
    reset_engine()
    reset_redis()
    get_settings.cache_clear()
    get_redis().flushdb()

    app = create_app()
    try:
        with TestClient(app) as client:
            yield client, app
    finally:
        app.dependency_overrides.clear()
        get_redis().flushdb()
        reset_redis()
        reset_engine()
        get_settings.cache_clear()


def _register(client: TestClient, prefix: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{prefix}-{uuid.uuid4().hex[:10]}@example.com",
            "password": "secure-password-123",
        },
    )
    assert response.status_code == 201, response.text
    user_id = uuid.UUID(response.json()["id"])

    workspace_response = client.get("/api/v1/workspaces")
    assert workspace_response.status_code == 200, workspace_response.text
    workspace_id = uuid.UUID(workspace_response.json()[0]["id"])

    csrf_response = client.get("/api/v1/auth/csrf")
    assert csrf_response.status_code == 200, csrf_response.text
    return user_id, workspace_id, csrf_response.json()["csrf_token"]


def _set_role(workspace_id: uuid.UUID, user_id: uuid.UUID, role: WorkspaceRole) -> None:
    with _db_session() as db:
        membership = db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
        )
        assert membership is not None
        membership.role = role
        db.commit()


def _add_member(workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
    with _db_session() as db:
        db.add(
            WorkspaceMember(
                workspace_id=workspace_id,
                user_id=user_id,
                role=WorkspaceRole.MEMBER,
            )
        )
        db.commit()


def _seed_with_opportunities(
    workspace_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed a scan and generate opportunities. Returns (project_id, scan_id, opportunity_id)."""
    project_id, scan_id, _ = _seed_full_pipeline(
        workspace_id,
        user_id,
        competitors=[("Rival", "rival.test")],
        mention_mode="competitor",
    )
    with _db_session() as db:
        ActionGenerationService(db).refresh_from_scan(workspace_id, project_id, scan_id)
        db.commit()
        opp = (
            db.execute(select(Opportunity).where(Opportunity.project_id == project_id))
            .scalars()
            .first()
        )
        assert opp is not None
        opp_id = opp.id
    return project_id, scan_id, opp_id


# ----------------------------------------------------------------------
# Competitor Explanation API Tests
# ----------------------------------------------------------------------


def test_api_list_competitor_summaries(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-list-comp")
    project_id, scan_id, _ = _seed_full_pipeline(
        workspace_id,
        user_id,
        competitors=[("Rival", "rival.test")],
    )

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}/competitors",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["scan_id"] == str(scan_id)
    assert len(data["competitors"]) == 1
    assert data["competitors"][0]["name"] == "Rival"


def test_api_get_competitor_explanation(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-get-comp")
    project_id, scan_id, comp_snap_id = _seed_full_pipeline(
        workspace_id,
        user_id,
        competitors=[("Rival", "rival.test")],
    )

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}"
        f"/competitors/{comp_snap_id}/explanation",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["competitor_name"] == "Rival"
    assert data["brand_name"] == "Acme"
    assert data["successful_observations"] == 5
    assert data["competitor_visibility_rate"] == "100.0000"
    assert data["brand_visibility_rate"] == "0.0000"
    assert "overlap" in data
    assert "provider_breakdown" in data


def test_api_competitor_explanation_tenant_isolation(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    owner_id, workspace_id, _ = _register(client, "p9api-tenant-owner")
    project_id, scan_id, comp_snap_id = _seed_full_pipeline(
        workspace_id,
        owner_id,
        competitors=[("Rival", "rival.test")],
    )

    _other_id, _other_workspace_id, other_csrf = _register(client, "p9api-tenant-other")

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}"
        f"/competitors/{comp_snap_id}/explanation",
        headers={"X-CSRF-Token": other_csrf},
    )
    assert response.status_code == 404, response.text


def test_api_competitor_explanation_member_can_read(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    owner_id, workspace_id, _ = _register(client, "p9api-comp-owner")
    project_id, scan_id, comp_snap_id = _seed_full_pipeline(
        workspace_id,
        owner_id,
        competitors=[("Rival", "rival.test")],
    )

    member_id, _, member_csrf = _register(client, "p9api-comp-member")
    _add_member(workspace_id, member_id)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}"
        f"/competitors/{comp_snap_id}/explanation",
        headers={"X-CSRF-Token": member_csrf},
    )
    assert response.status_code == 200, response.text


def test_api_competitor_explanation_unauthenticated_401(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    response = client.get(
        f"/api/v1/workspaces/{uuid.uuid4()}/projects/{uuid.uuid4()}/scans/{uuid.uuid4()}"
        f"/competitors/{uuid.uuid4()}/explanation"
    )
    assert response.status_code == 401, response.text


# ----------------------------------------------------------------------
# Opportunity API Tests
# ----------------------------------------------------------------------


def test_api_list_opportunities(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-list-opp")
    project_id, _scan_id, _ = _seed_with_opportunities(workspace_id, user_id)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] >= 1
    assert len(data["items"]) >= 1
    assert data["items"][0]["status"] == "OPEN"


def test_api_list_opportunities_with_filters(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-list-opp-filter")
    project_id, _scan_id, _ = _seed_with_opportunities(workspace_id, user_id)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities?status=OPEN",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert all(item["status"] == "OPEN" for item in data["items"])

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities?status=IN_PROGRESS",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 0


def test_api_get_opportunity_detail(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-get-opp")
    project_id, _scan_id, opp_id = _seed_with_opportunities(workspace_id, user_id)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opp_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["id"] == str(opp_id)
    assert "latest_occurrence" in data
    assert data["latest_occurrence"] is not None
    assert "evidence" in data["latest_occurrence"]
    assert len(data["latest_occurrence"]["evidence"]) > 0
    assert data["occurrence_count"] == 1


def test_api_get_opportunity_tenant_isolation(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    owner_id, workspace_id, _ = _register(client, "p9api-opp-tenant-owner")
    project_id, _scan_id, opp_id = _seed_with_opportunities(workspace_id, owner_id)

    _other_id, _other_workspace_id, other_csrf = _register(client, "p9api-opp-tenant-other")

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opp_id}",
        headers={"X-CSRF-Token": other_csrf},
    )
    assert response.status_code == 404, response.text


def test_api_get_opportunity_cross_project_404(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-opp-cross-proj")
    _project_id, _scan_id, opp_id = _seed_with_opportunities(workspace_id, user_id)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/projects/{uuid.uuid4()}/opportunities/{opp_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 404, response.text


# ----------------------------------------------------------------------
# Action Refresh API Tests
# ----------------------------------------------------------------------


def test_api_refresh_actions_owner(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-refresh-owner")
    project_id, scan_id, _ = _seed_full_pipeline(
        workspace_id,
        user_id,
        competitors=[("Rival", "rival.test")],
        mention_mode="competitor",
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}/actions/refresh",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["scan_id"] == str(scan_id)
    assert data["opportunities_detected"] >= 1
    assert data["opportunities_created"] >= 1
    assert data["action_engine_version"] == "deterministic-actions-v1"


def test_api_refresh_actions_admin(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-refresh-admin")
    _set_role(workspace_id, user_id, WorkspaceRole.ADMIN)
    project_id, scan_id, _ = _seed_full_pipeline(
        workspace_id,
        user_id,
        competitors=[("Rival", "rival.test")],
        mention_mode="competitor",
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}/actions/refresh",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text


def test_api_refresh_actions_member_forbidden(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    owner_id, workspace_id, _ = _register(client, "p9api-refresh-owner2")
    project_id, scan_id, _ = _seed_full_pipeline(
        workspace_id,
        owner_id,
        competitors=[("Rival", "rival.test")],
        mention_mode="competitor",
    )

    member_id, _, member_csrf = _register(client, "p9api-refresh-member")
    _add_member(workspace_id, member_id)

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}/actions/refresh",
        headers={"X-CSRF-Token": member_csrf},
    )
    assert response.status_code == 403, response.text


def test_api_refresh_actions_tenant_isolation(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    owner_id, workspace_id, _ = _register(client, "p9api-refresh-tenant-owner")
    project_id, scan_id, _ = _seed_full_pipeline(
        workspace_id,
        owner_id,
        competitors=[("Rival", "rival.test")],
        mention_mode="competitor",
    )

    _other_id, _other_workspace_id, other_csrf = _register(client, "p9api-refresh-tenant-other")

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}/actions/refresh",
        headers={"X-CSRF-Token": other_csrf},
    )
    assert response.status_code == 404, response.text


def test_api_refresh_actions_idempotent(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-refresh-idempotent")
    project_id, scan_id, _ = _seed_full_pipeline(
        workspace_id,
        user_id,
        competitors=[("Rival", "rival.test")],
        mention_mode="competitor",
    )

    r1 = client.post(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}/actions/refresh",
        headers={"X-CSRF-Token": csrf},
    )
    assert r1.status_code == 200, r1.text
    data1 = r1.json()
    assert data1["opportunities_created"] >= 1

    r2 = client.post(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}/actions/refresh",
        headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code == 200, r2.text
    data2 = r2.json()
    assert data2["opportunities_created"] == 0
    assert data2["occurrences_created"] == 0


# ----------------------------------------------------------------------
# Status Update API Tests
# ----------------------------------------------------------------------


def test_api_update_status_owner(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-status-owner")
    project_id, _scan_id, opp_id = _seed_with_opportunities(workspace_id, user_id)

    response = client.patch(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opp_id}",
        headers={"X-CSRF-Token": csrf},
        json={"status": "IN_PROGRESS"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "IN_PROGRESS"


def test_api_update_status_admin(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-status-admin")
    _set_role(workspace_id, user_id, WorkspaceRole.ADMIN)
    project_id, _scan_id, opp_id = _seed_with_opportunities(workspace_id, user_id)

    response = client.patch(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opp_id}",
        headers={"X-CSRF-Token": csrf},
        json={"status": "IN_PROGRESS"},
    )
    assert response.status_code == 200, response.text


def test_api_update_status_member_forbidden(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    owner_id, workspace_id, _ = _register(client, "p9api-status-owner2")
    project_id, _scan_id, opp_id = _seed_with_opportunities(workspace_id, owner_id)

    member_id, _, member_csrf = _register(client, "p9api-status-member")
    _add_member(workspace_id, member_id)

    response = client.patch(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opp_id}",
        headers={"X-CSRF-Token": member_csrf},
        json={"status": "IN_PROGRESS"},
    )
    assert response.status_code == 403, response.text


def test_api_update_status_verified_rejected(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-status-verified")
    project_id, _scan_id, opp_id = _seed_with_opportunities(workspace_id, user_id)

    response = client.patch(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opp_id}",
        headers={"X-CSRF-Token": csrf},
        json={"status": "VERIFIED"},
    )
    assert response.status_code == 422, response.text


def test_api_update_status_dismissed_with_reason(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-status-dismiss")
    project_id, _scan_id, opp_id = _seed_with_opportunities(workspace_id, user_id)

    response = client.patch(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opp_id}",
        headers={"X-CSRF-Token": csrf},
        json={"status": "DISMISSED", "dismissal_reason": "Not relevant"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "DISMISSED"


def test_api_update_status_tenant_isolation(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    owner_id, workspace_id, _ = _register(client, "p9api-status-tenant-owner")
    project_id, _scan_id, opp_id = _seed_with_opportunities(workspace_id, owner_id)

    _other_id, _other_workspace_id, other_csrf = _register(client, "p9api-status-tenant-other")

    response = client.patch(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/opportunities/{opp_id}",
        headers={"X-CSRF-Token": other_csrf},
        json={"status": "IN_PROGRESS"},
    )
    assert response.status_code == 404, response.text


def test_api_update_status_cross_project_404(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    user_id, workspace_id, csrf = _register(client, "p9api-status-cross-proj")
    _project_id, _scan_id, opp_id = _seed_with_opportunities(workspace_id, user_id)

    response = client.patch(
        f"/api/v1/workspaces/{workspace_id}/projects/{uuid.uuid4()}/opportunities/{opp_id}",
        headers={"X-CSRF-Token": csrf},
        json={"status": "IN_PROGRESS"},
    )
    assert response.status_code == 404, response.text
