"""API smoke tests for the project onboarding flow."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    LLMProvider,
)
from app.db.redis import reset_redis
from app.db.session import reset_engine
from app.main import create_app
from app.models import (
    BillingAccount,
    PlanDefinition,
    PlanProvider,
)

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


def _unique_email() -> str:
    return f"p4api-{uuid.uuid4().hex[:8]}@example.com"


def _direct_db_session() -> Session:
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return factory()


def _setup_plan_and_billing(
    db: Session,
    ws_id: uuid.UUID,
    providers: list[LLMProvider] | None = None,
    max_projects: int = 5,
) -> None:
    plan_code = f"P4API_{ws_id.hex[:8]}"
    plan = PlanDefinition(
        code=plan_code,
        name=f"P4 API Test {plan_code}",
        is_active=True,
        max_projects=max_projects,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
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
            workspace_id=ws_id,
            source=BillingSource.ADMIN,
            status=BillingAccountStatus.ACTIVE,
            plan_code=plan_code,
            is_primary=True,
        )
    )
    db.commit()


@pytest.fixture()
def client():
    reset_engine()
    reset_redis()
    from app.config import get_settings

    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_redis()


@pytest.fixture()
def clean_redis():
    from app.db.redis import get_redis

    redis = get_redis()
    redis.flushdb()
    yield
    redis.flushdb()


def _register_and_get_csrf(
    client: TestClient, email: str | None = None
) -> tuple[TestClient, str, str, str]:
    """Register a user and return (client, csrf_token, user_id, ws_id)."""
    if email is None:
        email = _unique_email()
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "secure-password-123"},
    )
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["id"]
    ws_resp = client.get("/api/v1/workspaces")
    assert ws_resp.status_code == 200
    ws_id = ws_resp.json()[0]["id"]
    csrf_resp = client.get("/api/v1/auth/csrf")
    assert csrf_resp.status_code == 200
    csrf_token = csrf_resp.json()["csrf_token"]
    return client, csrf_token, user_id, ws_id


@pytest.mark.integration
class TestProjectAPISmoke:
    """End-to-end API smoke test for project onboarding and management."""

    def test_full_onboarding_flow(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, csrf_token, _, ws_id = _register_and_get_csrf(client)

        # Setup plan + billing via direct DB.
        db = _direct_db_session()
        try:
            _setup_plan_and_billing(db, uuid.UUID(ws_id))
        finally:
            db.close()

        base = f"/api/v1/workspaces/{ws_id}/projects"
        h = {"X-CSRF-Token": csrf_token}

        # 1. Create project via onboarding endpoint.
        resp = client.post(
            base,
            headers=h,
            json={
                "name": "Acme Project",
                "domain": "acme.com",
                "brand_name": "Acme",
                "brand_aliases": ["Acme Inc"],
                "industry": "SaaS",
                "target_country": "US",
                "target_language": "en",
                "target_audience": "Small Businesses",
                "keywords": [{"text": "best crm"}],
                "competitors": [{"name": "Mailchimp", "domain": "mailchimp.com"}],
                "providers": ["OPENAI"],
            },
        )
        assert resp.status_code == 201, resp.text
        project = resp.json()
        pid = project["id"]
        assert project["name"] == "Acme Project"
        assert project["status"] == "ACTIVE"
        assert project["prompt_input_revision"] == 1

        # 2. Get project summary.
        resp = client.get(f"{base}/{pid}")
        assert resp.status_code == 200
        summary = resp.json()
        assert summary["keyword_count"] == 1
        assert summary["competitor_count"] == 1
        assert summary["enabled_provider_count"] == 1
        assert summary["current_prompt_set_version"] == 1
        assert summary["is_prompt_set_stale"] is False

        # 3. List keywords.
        resp = client.get(f"{base}/{pid}/keywords")
        assert resp.status_code == 200
        keywords = resp.json()
        assert len(keywords) == 1
        assert keywords[0]["text"] == "best crm"

        # 4. Add a keyword.
        resp = client.post(
            f"{base}/{pid}/keywords",
            headers=h,
            json={"text": "email marketing"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["text"] == "email marketing"

        # 5. List competitors.
        resp = client.get(f"{base}/{pid}/competitors")
        assert resp.status_code == 200
        competitors = resp.json()
        assert len(competitors) == 1
        assert competitors[0]["domain"] == "mailchimp.com"

        # 6. List providers.
        resp = client.get(f"{base}/{pid}/providers")
        assert resp.status_code == 200
        providers = resp.json()
        assert len(providers) == 1
        assert providers[0]["provider"] == "OPENAI"

        # 7. List prompt sets.
        resp = client.get(f"{base}/{pid}/prompt-sets")
        assert resp.status_code == 200
        prompt_sets = resp.json()
        assert len(prompt_sets) == 1
        assert prompt_sets[0]["version"] == 1
        assert prompt_sets[0]["status"] == "ACTIVE"

        # 8. Update project (brand name change → stale).
        resp = client.patch(
            f"{base}/{pid}",
            headers=h,
            json={"brand_name": "NewBrand"},
        )
        assert resp.status_code == 200
        assert resp.json()["brand_name"] == "NewBrand"

        # 9. Check staleness.
        resp = client.get(f"{base}/{pid}")
        summary = resp.json()
        assert summary["is_prompt_set_stale"] is True

        # 10. Regenerate prompt set.
        resp = client.post(f"{base}/{pid}/prompt-sets/regenerate", headers=h)
        assert resp.status_code == 200, resp.text
        new_set = resp.json()
        assert new_set["version"] == 2
        assert new_set["status"] == "ACTIVE"

        # 11. Pause project.
        resp = client.post(f"{base}/{pid}/pause", headers=h)
        assert resp.status_code == 200
        assert resp.json()["status"] == "PAUSED"

        # 12. Activate project.
        resp = client.post(f"{base}/{pid}/activate", headers=h)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ACTIVE"

        # 13. Archive project.
        resp = client.post(f"{base}/{pid}/archive", headers=h)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ARCHIVED"

    def test_cross_workspace_isolation(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        """Project from workspace A cannot be accessed via workspace B."""
        # User A.
        client, csrf_a, _, ws_a_id = _register_and_get_csrf(client)
        db = _direct_db_session()
        try:
            _setup_plan_and_billing(db, uuid.UUID(ws_a_id))
        finally:
            db.close()

        base_a = f"/api/v1/workspaces/{ws_a_id}/projects"
        h_a = {"X-CSRF-Token": csrf_a}

        # Create project in workspace A.
        resp = client.post(
            base_a,
            headers=h_a,
            json={
                "name": "Project A",
                "domain": "a.com",
                "brand_name": "BrandA",
                "target_language": "en",
                "keywords": [{"text": "test kw"}],
                "providers": ["OPENAI"],
            },
        )
        assert resp.status_code == 201, resp.text
        project_a_id = resp.json()["id"]

        # User B registers (new session).
        client, csrf_b, _, ws_b_id = _register_and_get_csrf(client)
        db = _direct_db_session()
        try:
            _setup_plan_and_billing(db, uuid.UUID(ws_b_id))
        finally:
            db.close()

        base_b = f"/api/v1/workspaces/{ws_b_id}/projects"

        # Try to access project A from workspace B (different workspace path).
        resp = client.get(f"{base_b}/{project_a_id}")
        assert resp.status_code in (404, 409)

        # Try to access from workspace A's path but as user B (not member).
        resp = client.get(f"{base_a}/{project_a_id}")
        assert resp.status_code in (404, 409)

    def test_onboarding_validation_errors(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, csrf_token, _, ws_id = _register_and_get_csrf(client)
        db = _direct_db_session()
        try:
            _setup_plan_and_billing(db, uuid.UUID(ws_id))
        finally:
            db.close()

        base = f"/api/v1/workspaces/{ws_id}/projects"
        h = {"X-CSRF-Token": csrf_token}

        # Missing required fields.
        resp = client.post(base, headers=h, json={"name": "Incomplete"})
        assert resp.status_code == 422

        # Empty keywords.
        resp = client.post(
            base,
            headers=h,
            json={
                "name": "No Keywords",
                "domain": "nkw.com",
                "brand_name": "Nkw",
                "target_language": "en",
                "keywords": [],
                "providers": ["OPENAI"],
            },
        )
        assert resp.status_code == 422

        # Empty providers.
        resp = client.post(
            base,
            headers=h,
            json={
                "name": "No Providers",
                "domain": "npv.com",
                "brand_name": "Npv",
                "target_language": "en",
                "keywords": [{"text": "test"}],
                "providers": [],
            },
        )
        assert resp.status_code == 422

    def test_unauthenticated_access(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        base = f"/api/v1/workspaces/{uuid.uuid4()}/projects"
        resp = client.get(base)
        assert resp.status_code == 401

    def test_list_projects(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, csrf_token, _, ws_id = _register_and_get_csrf(client)
        db = _direct_db_session()
        try:
            _setup_plan_and_billing(db, uuid.UUID(ws_id))
        finally:
            db.close()

        base = f"/api/v1/workspaces/{ws_id}/projects"
        h = {"X-CSRF-Token": csrf_token}

        # Initially empty.
        resp = client.get(base)
        assert resp.status_code == 200
        assert resp.json() == []

        # Create a project.
        resp = client.post(
            base,
            headers=h,
            json={
                "name": "Test Project",
                "domain": "test.com",
                "brand_name": "AcmeCorp",
                "target_country": "US",
                "target_language": "en",
                "keywords": [{"text": "best crm"}],
                "providers": ["OPENAI"],
            },
        )
        assert resp.status_code == 201, resp.text

        # List should have one project.
        resp = client.get(base)
        assert resp.status_code == 200
        projects = resp.json()
        assert len(projects) == 1
        assert projects[0]["name"] == "Test Project"
