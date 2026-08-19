"""Integration coverage for Phase 6 quota accounting metadata."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    CostSource,
    LLMProvider,
    PromptRunStatus,
    PromptType,
    ProviderExecutionMode,
    ProviderSurface,
    ScanStatus,
    ScanType,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import ConflictError
from app.models import (
    BillingAccount,
    PlanDefinition,
    PlanProvider,
    Project,
    ProjectKeyword,
    Prompt,
    PromptRun,
    PromptSet,
    ProviderPriceRule,
    Scan,
    User,
    Workspace,
)
from app.models.quota_reservation import QuotaReservation
from app.models.workspace import WorkspaceMember
from app.services.quota_service import QuotaService


def _make_workspace_with_quota(db: Session) -> tuple[Workspace, User]:
    suffix = uuid.uuid4().hex
    user = User(email=f"phase6-{suffix}@example.com", password_hash="h")
    workspace = Workspace(name=f"Phase 6 {suffix}", workspace_type=WorkspaceType.AGENCY)
    db.add_all([user, workspace])
    db.flush()
    db.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
    )

    plan = PlanDefinition(
        code=f"PHASE6_{suffix}",
        name="Phase 6 Test Plan",
        is_active=True,
        max_projects=5,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
        max_team_members=5,
        monthly_ai_checks=100,
    )
    db.add(plan)
    db.flush()
    db.add_all(
        [
            PlanProvider(plan_id=plan.id, provider=LLMProvider.OPENAI),
            BillingAccount(
                workspace_id=workspace.id,
                source=BillingSource.ADMIN,
                status=BillingAccountStatus.ACTIVE,
                plan_code=plan.code,
                is_primary=True,
            ),
        ]
    )
    db.flush()
    return workspace, user


def _make_price_rule(db: Session, suffix: str) -> ProviderPriceRule:
    now = datetime.now(UTC)
    rule = ProviderPriceRule(
        pricing_key=f"phase6-{suffix}-{uuid.uuid4().hex}",
        provider=LLMProvider.OPENAI,
        provider_surface=ProviderSurface.OPENAI_RESPONSES_API,
        model=f"gpt-phase6-{suffix}",
        effective_from=now - timedelta(days=1),
        input_per_million_usd=Decimal("1.25"),
        cached_input_per_million_usd=Decimal("0.25"),
        cache_write_per_million_usd=Decimal("1.50"),
        output_per_million_usd=Decimal("5.00"),
        reasoning_per_million_usd=Decimal("5.00"),
        citation_per_million_usd=Decimal("0.10"),
        search_per_1000_usd=Decimal("10.00"),
        request_fee_usd=Decimal("0.001"),
        input_tokens_include_cached=True,
        output_tokens_include_reasoning=True,
        verified_at=now,
        source_url="https://example.com/synthetic-pricing",
    )
    db.add(rule)
    db.flush()
    return rule


def _make_accounting_graph(
    db: Session,
) -> tuple[
    QuotaService, QuotaReservation, PromptRun, PromptRun, ProviderPriceRule, ProviderPriceRule
]:
    workspace, user = _make_workspace_with_quota(db)
    project = Project(
        workspace_id=workspace.id,
        name="Phase 6 Project",
        domain=f"{uuid.uuid4().hex}.example.com",
        brand_name="Phase 6 Brand",
    )
    db.add(project)
    db.flush()

    service = QuotaService(db)
    reservation = service.reserve_ai_checks(
        workspace.id,
        requested_checks=1,
        idempotency_key=f"phase6-reserve-{uuid.uuid4().hex}",
        user_id=user.id,
        project_id=project.id,
    )

    prompt_set = PromptSet(
        project_id=project.id,
        version=1,
        input_revision=1,
        generator_key="phase6-synthetic",
        created_by_user_id=user.id,
    )
    keyword = ProjectKeyword(
        project_id=project.id,
        text="synthetic quota accounting",
        normalized_text=f"synthetic quota accounting {uuid.uuid4().hex}",
    )
    db.add_all([prompt_set, keyword])
    db.flush()
    prompt = Prompt(
        prompt_set_id=prompt_set.id,
        project_keyword_id=keyword.id,
        variant_index=1,
        text="What metadata must quota accounting preserve?",
        prompt_type=PromptType.NON_BRANDED,
    )
    db.add(prompt)
    db.flush()
    scan = Scan(
        workspace_id=workspace.id,
        project_id=project.id,
        prompt_set_id=prompt_set.id,
        scan_type=ScanType.STANDARD,
        status=ScanStatus.RUNNING,
        requested_by_user_id=user.id,
        idempotency_key=f"phase6-scan-{uuid.uuid4().hex}",
        quota_reservation_id=reservation.id,
        prompt_count=1,
        provider_count=1,
        planned_ai_checks=1,
        successful_runs=0,
        failed_runs=0,
    )
    db.add(scan)
    db.flush()

    runs = [
        PromptRun(
            scan_id=scan.id,
            prompt_id=prompt.id,
            provider=LLMProvider.OPENAI,
            provider_surface=ProviderSurface.OPENAI_RESPONSES_API,
            execution_mode=ProviderExecutionMode.WEB_GROUNDED,
            requested_model="gpt-phase6",
            status=PromptRunStatus.SUCCEEDED,
            attempt_number=attempt,
        )
        for attempt in (1, 2)
    ]
    db.add_all(runs)
    primary_rule = _make_price_rule(db, "primary")
    alternate_rule = _make_price_rule(db, "alternate")
    db.flush()
    return service, reservation, runs[0], runs[1], primary_rule, alternate_rule


def _phase6_payload(
    prompt_run: PromptRun,
    pricing_rule: ProviderPriceRule,
) -> dict[str, Any]:
    return {
        "provider": "OPENAI",
        "model": "gpt-phase6",
        "input_tokens": 101,
        "output_tokens": 59,
        "total_tokens": 160,
        "cached_input_tokens": 17,
        "cache_write_input_tokens": 13,
        "reasoning_tokens": 19,
        "citation_tokens": 7,
        "search_requests": 3,
        "cost_usd": Decimal("0.0123456789"),
        "provider_reported_cost_usd": Decimal("0.0130000000"),
        "cost_source": CostSource.PRICE_RULE,
        "pricing_rule_id": pricing_rule.id,
        "prompt_run_id": prompt_run.id,
    }


@pytest.mark.integration
class TestPhase6ReservationTTL:
    def test_reserve_ai_checks_honors_ttl_seconds_override(self, db_session: Session) -> None:
        workspace, _ = _make_workspace_with_quota(db_session)
        now = datetime(2026, 3, 15, 12, 30, tzinfo=UTC)

        reservation = QuotaService(db_session, clock=now).reserve_ai_checks(
            workspace.id,
            requested_checks=1,
            idempotency_key=f"phase6-ttl-{uuid.uuid4().hex}",
            ttl_seconds=37,
        )

        assert reservation.expires_at == now + timedelta(seconds=37)

    @pytest.mark.parametrize("ttl_seconds", [0, -1])
    def test_reserve_ai_checks_rejects_nonpositive_ttl(
        self, db_session: Session, ttl_seconds: int
    ) -> None:
        workspace, _ = _make_workspace_with_quota(db_session)

        with pytest.raises(ValueError, match="ttl_seconds must be positive"):
            QuotaService(db_session).reserve_ai_checks(
                workspace.id,
                requested_checks=1,
                idempotency_key=f"phase6-invalid-ttl-{uuid.uuid4().hex}",
                ttl_seconds=ttl_seconds,
            )


@pytest.mark.integration
class TestPhase6CommitMetadata:
    def test_commit_persists_all_metadata_and_exact_replay_returns_same_event(
        self, db_session: Session
    ) -> None:
        service, reservation, prompt_run, _, pricing_rule, _ = _make_accounting_graph(db_session)
        payload = _phase6_payload(prompt_run, pricing_rule)
        usage_key = f"phase6-usage-{uuid.uuid4().hex}"

        event = service.commit_ai_checks(
            reservation.id,
            quantity=1,
            usage_idempotency_key=usage_key,
            **payload,
        )
        replayed = service.commit_ai_checks(
            reservation.id,
            quantity=1,
            usage_idempotency_key=usage_key,
            **payload,
        )

        assert replayed is event
        assert event.cached_input_tokens == 17
        assert event.cache_write_input_tokens == 13
        assert event.reasoning_tokens == 19
        assert event.citation_tokens == 7
        assert event.search_requests == 3
        assert event.provider_reported_cost_usd == Decimal("0.0130000000")
        assert event.cost_usd == Decimal("0.0123456789")
        assert event.cost_source == CostSource.PRICE_RULE
        assert event.pricing_rule_id == pricing_rule.id
        assert event.prompt_run_id == prompt_run.id

    @pytest.mark.parametrize(
        "field,changed_value",
        [
            ("cached_input_tokens", 18),
            ("cache_write_input_tokens", 14),
            ("reasoning_tokens", 20),
            ("citation_tokens", 8),
            ("search_requests", 4),
            ("cost_source", CostSource.UNKNOWN),
            ("pricing_rule_id", "alternate"),
            ("prompt_run_id", "alternate"),
        ],
    )
    def test_materially_different_phase6_replay_raises_conflict(
        self,
        db_session: Session,
        field: str,
        changed_value: Any,
    ) -> None:
        service, reservation, prompt_run, alternate_run, pricing_rule, alternate_rule = (
            _make_accounting_graph(db_session)
        )
        payload = _phase6_payload(prompt_run, pricing_rule)
        usage_key = f"phase6-conflict-{uuid.uuid4().hex}"
        service.commit_ai_checks(
            reservation.id,
            quantity=1,
            usage_idempotency_key=usage_key,
            **payload,
        )
        changed_payload = dict(payload)
        if field == "pricing_rule_id":
            changed_value = alternate_rule.id
        elif field == "prompt_run_id":
            changed_value = alternate_run.id
        changed_payload[field] = changed_value

        with pytest.raises(ConflictError, match="conflicting parameters"):
            service.commit_ai_checks(
                reservation.id,
                quantity=1,
                usage_idempotency_key=usage_key,
                **changed_payload,
            )

    def test_unknown_cost_none_is_persisted_as_sql_null(self, db_session: Session) -> None:
        service, reservation, prompt_run, _, _, _ = _make_accounting_graph(db_session)

        event = service.commit_ai_checks(
            reservation.id,
            quantity=1,
            usage_idempotency_key=f"phase6-unknown-cost-{uuid.uuid4().hex}",
            provider="OPENAI",
            model="gpt-phase6",
            cost_usd=None,
            cost_source=CostSource.UNKNOWN,
            prompt_run_id=prompt_run.id,
        )

        stored_cost = db_session.execute(
            text("SELECT cost_usd FROM usage_events WHERE id = :id"),
            {"id": event.id},
        ).scalar_one()
        assert event.cost_usd is None
        assert stored_cost is None
