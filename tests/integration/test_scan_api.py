"""Integration coverage for the Phase 6 Scan API.

The API uses real PostgreSQL state, but scan dispatch is dependency-overridden so
these tests never publish a Celery task or execute an AI provider request.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    CostSource,
    LLMProvider,
    ProjectStatus,
    PromptSetStatus,
    PromptType,
    WorkspaceRole,
)
from app.db.redis import get_redis, reset_redis
from app.db.session import reset_engine
from app.main import create_app
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
    QuotaReservation,
    WorkspaceMember,
)
from app.routers.api import scans as scans_router
from app.services.prompt_generation_service import GENERATOR_KEY

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)

_INTERNAL_SCAN_FIELDS = {
    "workspace_id",
    "requested_by_user_id",
    "idempotency_key",
    "quota_reservation_id",
    "dispatched_at",
    "failure_code",
    "failure_message",
}
_INTERNAL_RUN_FIELDS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "reasoning_tokens",
    "citation_tokens",
    "search_requests",
    "provider_reported_cost_usd",
    "calculated_cost_usd",
    "cost_usd",
    "cost_source",
    "pricing_rule_id",
    "usage_event_id",
}


@dataclass
class FakeScanDispatcher:
    dispatched_scan_ids: list[uuid.UUID] = field(default_factory=list)

    def dispatch(self, scan_id: uuid.UUID) -> None:
        self.dispatched_scan_ids.append(scan_id)


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


@pytest.fixture()
def api(
    prepared_test_db: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, FastAPI, FakeScanDispatcher]]:
    assert prepared_test_db == TEST_DATABASE_URL
    # Registry preflight validates configuration but does not make a provider call.
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-never-sent")
    monkeypatch.setenv("OPENAI_SCAN_MODEL", "scan-api-test-model")
    monkeypatch.setenv("PRICING_REQUIRE_RULE_FOR_EXECUTION", "false")
    reset_engine()
    reset_redis()
    get_settings.cache_clear()
    get_redis().flushdb()

    dispatcher = FakeScanDispatcher()
    app = create_app()
    app.dependency_overrides[scans_router.get_scan_dispatcher] = lambda: dispatcher
    try:
        with TestClient(app) as client:
            yield client, app, dispatcher
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


def _seed_scan_prerequisites(workspace_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    """Create the smallest entitled, fresh, one-prompt/one-provider scan plan."""
    with _db_session() as db:
        plan_code = f"SCAN_API_{workspace_id.hex[:12]}"
        plan = PlanDefinition(
            code=plan_code,
            name=f"Scan API {plan_code}",
            is_active=True,
            max_projects=5,
            max_keywords_per_project=5,
            max_competitors_per_project=0,
            max_team_members=5,
            monthly_ai_checks=100,
        )
        db.add(plan)
        db.flush()
        db.add(PlanProvider(plan_id=plan.id, provider=LLMProvider.OPENAI))
        db.add(
            BillingAccount(
                workspace_id=workspace_id,
                source=BillingSource.ADMIN,
                status=BillingAccountStatus.ACTIVE,
                plan_code=plan_code,
                is_primary=True,
            )
        )

        project = Project(
            workspace_id=workspace_id,
            name="Scan API Project",
            domain=f"scan-{uuid.uuid4().hex[:8]}.example",
            brand_name="Scan API Brand",
            brand_aliases=[],
            target_language="en",
            status=ProjectStatus.ACTIVE,
            prompt_input_revision=1,
        )
        db.add(project)
        db.flush()
        db.add(
            ProjectProvider(
                project_id=project.id,
                provider=LLMProvider.OPENAI,
                enabled=True,
            )
        )
        keyword = ProjectKeyword(
            project_id=project.id,
            text="integration analytics",
            normalized_text=f"integration analytics {uuid.uuid4().hex[:8]}",
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
            created_by_user_id=user_id,
        )
        db.add(prompt_set)
        db.flush()
        db.add(
            Prompt(
                prompt_set_id=prompt_set.id,
                project_keyword_id=keyword.id,
                variant_index=1,
                text="What are the best integration analytics tools?",
                prompt_type=PromptType.NON_BRANDED,
                target_language="en",
                commercial_intent=True,
                active=True,
            )
        )
        db.commit()
        return project.id


def _seed_other_project(workspace_id: uuid.UUID) -> uuid.UUID:
    with _db_session() as db:
        project = Project(
            workspace_id=workspace_id,
            name="Other Project",
            domain=f"other-{uuid.uuid4().hex[:8]}.example",
            brand_name="Other Brand",
            brand_aliases=[],
            target_language="en",
            status=ProjectStatus.ACTIVE,
            prompt_input_revision=1,
        )
        db.add(project)
        db.commit()
        return project.id


def _set_internal_run_costs(scan_id: uuid.UUID) -> uuid.UUID:
    with _db_session() as db:
        run = db.scalar(select(PromptRun).where(PromptRun.scan_id == scan_id))
        assert run is not None
        run.input_tokens = 11
        run.output_tokens = 7
        run.total_tokens = 18
        run.search_requests = 1
        run.provider_reported_cost_usd = Decimal("0.123")
        run.calculated_cost_usd = Decimal("0.100")
        run.cost_usd = Decimal("0.123")
        run.cost_source = CostSource.PROVIDER_REPORTED
        db.commit()
        return run.id


def _assert_no_internal_fields(payload: object) -> None:
    if isinstance(payload, dict):
        forbidden = (_INTERNAL_SCAN_FIELDS | _INTERNAL_RUN_FIELDS) & payload.keys()
        assert not forbidden, f"customer response exposed internal fields: {sorted(forbidden)}"
        for value in payload.values():
            _assert_no_internal_fields(value)
    elif isinstance(payload, list):
        for value in payload:
            _assert_no_internal_fields(value)


@pytest.mark.integration
@pytest.mark.parametrize("role", [WorkspaceRole.OWNER, WorkspaceRole.ADMIN])
def test_owner_and_admin_can_create_scan_with_202(api, role: WorkspaceRole) -> None:  # type: ignore[no-untyped-def]
    client, _, dispatcher = api
    user_id, workspace_id, csrf = _register(client, f"scan-{role.value.lower()}")
    if role != WorkspaceRole.OWNER:
        _set_role(workspace_id, user_id, role)
    project_id = _seed_scan_prerequisites(workspace_id, user_id)

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": f"{role.value}-create"},
        json={"scan_type": "STANDARD"},
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "PENDING"
    assert dispatcher.dispatched_scan_ids == [uuid.UUID(response.json()["id"])]


@pytest.mark.integration
def test_scan_api_permissions_idempotency_safe_reads_and_tenant_scoping(api) -> None:  # type: ignore[no-untyped-def]
    client, _, dispatcher = api
    owner_id, workspace_id, owner_csrf = _register(client, "scan-owner-flow")
    project_id = _seed_scan_prerequisites(workspace_id, owner_id)
    base = f"/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans"

    missing_key = client.post(
        base,
        headers={"X-CSRF-Token": owner_csrf},
        json={"scan_type": "STANDARD"},
    )
    assert missing_key.status_code == 422, missing_key.text

    first = client.post(
        base,
        headers={"X-CSRF-Token": owner_csrf, "Idempotency-Key": "stable-key"},
        json={"scan_type": "STANDARD"},
    )
    assert first.status_code == 202, first.text
    first_scan_id = uuid.UUID(first.json()["id"])

    second = client.post(
        base,
        headers={"X-CSRF-Token": owner_csrf, "Idempotency-Key": "second-key"},
        json={"scan_type": "STANDARD"},
    )
    assert second.status_code == 202, second.text
    second_scan_id = uuid.UUID(second.json()["id"])
    first_run_id = _set_internal_run_costs(first_scan_id)

    dispatches_before_retry = list(dispatcher.dispatched_scan_ids)
    with _db_session() as db:
        reservations_before_retry = db.scalar(select(func.count()).select_from(QuotaReservation))

    duplicate = client.post(
        base,
        headers={"X-CSRF-Token": owner_csrf, "Idempotency-Key": " stable-key "},
        json={"scan_type": "STANDARD"},
    )
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["id"] == str(first_scan_id)
    assert dispatcher.dispatched_scan_ids == dispatches_before_retry
    with _db_session() as db:
        reservations_after_retry = db.scalar(select(func.count()).select_from(QuotaReservation))
    assert reservations_after_retry == reservations_before_retry

    member_id, member_workspace_id, member_csrf = _register(client, "scan-member")
    _add_member(workspace_id, member_id)

    member_create = client.post(
        base,
        headers={"X-CSRF-Token": member_csrf, "Idempotency-Key": "member-forbidden"},
        json={"scan_type": "STANDARD"},
    )
    assert member_create.status_code == 403, member_create.text
    assert dispatcher.dispatched_scan_ids == dispatches_before_retry

    list_response = client.get(base)
    detail_response = client.get(f"{base}/{first_scan_id}")
    runs_response = client.get(f"{base}/{first_scan_id}/runs")
    run_detail_response = client.get(f"{base}/{first_scan_id}/runs/{first_run_id}")
    for response in (list_response, detail_response, runs_response, run_detail_response):
        assert response.status_code == 200, response.text
        _assert_no_internal_fields(response.json())
    assert {item["id"] for item in list_response.json()["items"]} == {
        str(first_scan_id),
        str(second_scan_id),
    }
    assert detail_response.json()["runs"][0]["id"] == str(first_run_id)
    assert runs_response.json()[0]["id"] == str(first_run_id)
    assert run_detail_response.json()["id"] == str(first_run_id)

    other_project_id = _seed_other_project(workspace_id)
    wrong_project = client.get(
        f"/api/v1/workspaces/{workspace_id}/projects/{other_project_id}/scans/{first_scan_id}"
    )
    assert wrong_project.status_code == 404, wrong_project.text

    foreign_workspace = client.get(
        f"/api/v1/workspaces/{member_workspace_id}/projects/{project_id}/scans/{first_scan_id}"
    )
    assert foreign_workspace.status_code == 404, foreign_workspace.text

    foreign_run = client.get(f"{base}/{second_scan_id}/runs/{first_run_id}")
    assert foreign_run.status_code == 404, foreign_run.text
