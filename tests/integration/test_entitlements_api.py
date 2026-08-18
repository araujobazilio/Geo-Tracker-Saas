"""API integration tests for entitlements and usage endpoints.

Tests:
  - GET /entitlements returns product capabilities
  - GET /usage returns monthly quota state
  - Cross-tenant access returns 404
  - Unauthenticated access returns 401
  - UNENTITLED workspace returns zero limits
"""

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
    return f"ent-api-{uuid.uuid4().hex[:8]}@example.com"


def _direct_db_session() -> Session:
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return factory()


def _setup_plan_and_billing(
    db: Session,
    ws_id: uuid.UUID,
    plan_code: str | None = None,
    monthly_ai_checks: int = 100,
    providers: list[LLMProvider] | None = None,
) -> None:
    if plan_code is None:
        plan_code = f"API_TEST_PLAN_{ws_id.hex[:8]}"
    if providers is None:
        providers = [LLMProvider.OPENAI, LLMProvider.ANTHROPIC]
    plan = PlanDefinition(
        code=plan_code,
        name=f"API Test {plan_code}",
        is_active=True,
        max_projects=5,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
        max_team_members=5,
        monthly_ai_checks=monthly_ai_checks,
        confidence_scans_enabled=True,
        white_label_reports=True,
    )
    db.add(plan)
    db.flush()
    for p in providers:
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


def _register_and_get_ws_id(client, email: str | None = None) -> tuple[TestClient, str, str]:  # type: ignore[no-untyped-def]
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
    return client, user_id, ws_id


@pytest.mark.integration
class TestEntitlementsAPI:
    def test_get_entitlements_authenticated(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, _, ws_id = _register_and_get_ws_id(client)
        # Setup plan + billing via direct DB.
        db = _direct_db_session()
        try:
            _setup_plan_and_billing(db, uuid.UUID(ws_id))
        finally:
            db.close()

        resp = client.get(f"/api/v1/workspaces/{ws_id}/entitlements")
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_code"].startswith("API_TEST_PLAN")
        assert data["monthly_ai_checks"] == 100
        assert data["max_projects"] == 5
        assert "OPENAI" in data["allowed_providers"]
        assert "ANTHROPIC" in data["allowed_providers"]
        assert data["confidence_scans_enabled"] is True
        # Should not expose billing internals.
        assert "external_customer_id" not in data

    def test_get_entitlements_unentitled_workspace(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, _, ws_id = _register_and_get_ws_id(client)
        # No plan/billing setup → UNENTITLED.
        resp = client.get(f"/api/v1/workspaces/{ws_id}/entitlements")
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan_code"] == "UNENTITLED"
        assert data["monthly_ai_checks"] == 0
        assert data["allowed_providers"] == []

    def test_get_entitlements_unauthenticated(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        resp = client.get(f"/api/v1/workspaces/{uuid.uuid4()}/entitlements")
        assert resp.status_code == 401

    def test_get_entitlements_cross_tenant_404(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        # User A.
        client, _, ws_a_id = _register_and_get_ws_id(client)
        # User B.
        client, _, _ = _register_and_get_ws_id(client)

        # User B tries to access User A's entitlements.
        resp = client.get(f"/api/v1/workspaces/{ws_a_id}/entitlements")
        assert resp.status_code == 404


@pytest.mark.integration
class TestUsageAPI:
    def test_get_usage_authenticated(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, _, ws_id = _register_and_get_ws_id(client)
        db = _direct_db_session()
        try:
            _setup_plan_and_billing(db, uuid.UUID(ws_id), monthly_ai_checks=200)
        finally:
            db.close()

        resp = client.get(f"/api/v1/workspaces/{ws_id}/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 200
        assert data["used"] == 0
        assert data["reserved"] == 0
        assert data["remaining"] == 200
        assert "period_start" in data
        assert "period_end" in data

    def test_get_usage_unentitled(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, _, ws_id = _register_and_get_ws_id(client)
        resp = client.get(f"/api/v1/workspaces/{ws_id}/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 0
        assert data["remaining"] == 0

    def test_get_usage_cross_tenant_404(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        client, _, ws_a_id = _register_and_get_ws_id(client)
        client, _, _ = _register_and_get_ws_id(client)
        resp = client.get(f"/api/v1/workspaces/{ws_a_id}/usage")
        assert resp.status_code == 404

    def test_get_usage_unauthenticated(self, client, clean_redis) -> None:  # type: ignore[no-untyped-def]
        resp = client.get(f"/api/v1/workspaces/{uuid.uuid4()}/usage")
        assert resp.status_code == 401
