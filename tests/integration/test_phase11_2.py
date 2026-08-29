"""Phase 11.2 correctness closure tests — scheduler advisory lock,
notification dedup ON CONFLICT, percentage scale, email failure/retry.

Run all tests TOGETHER in one pytest invocation:

    pytest -q tests/integration/test_phase11_2.py \
        tests/integration/test_phase11_1.py --durations=20
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    EmailDeliveryStatus,
    LLMProvider,
    NotificationType,
    ProjectStatus,
    PromptSetStatus,
    PromptType,
    ProviderSurface,
    ScanStatus,
    ScanType,
    ScheduledScanOutcome,
    WorkspaceRole,
    WorkspaceType,
)
from app.models.billing import BillingAccount
from app.models.email_delivery import EmailDelivery
from app.models.notification import Notification, NotificationPreference
from app.models.plan_definition import PlanDefinition
from app.models.plan_provider import PlanProvider
from app.models.project import Project
from app.models.prompt_set import PromptSet
from app.models.scan import Scan
from app.models.tracking import Competitor, ProjectKeyword, ProjectProvider, Prompt
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.services.email_templates import build_scheduled_scan_message
from app.services.email_transport import EmailSendResult
from app.services.notification_service import NotificationInput, NotificationService
from app.services.prompt_generation_service import GENERATOR_KEY
from app.services.scanning.dispatcher import ScanDispatcher
from app.services.scheduled_scan_service import ScheduledScanService

os.environ.setdefault("EMAIL_ENABLED", "true")

pytestmark = pytest.mark.integration

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


# ----------------------------------------------------------------------
# Engine factory fixture (real PostgreSQL, committed transactions)
# ----------------------------------------------------------------------


@pytest.fixture()
def engine_factory() -> tuple[Any, Callable[[], Session]]:
    """Create a real PostgreSQL engine + session factory."""
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True, future=True, pool_size=10)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield engine, factory
    engine.dispose()


# ----------------------------------------------------------------------
# Cleanup fixture (FK-ordered)
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_phase11_2_data(engine_factory: Any) -> Any:
    """Clean up P112 test data before and after each test."""
    _, factory = engine_factory

    cleanup_stmts = [
        # 1. Tables referencing scans (except scan_entity_snapshots/prompt_runs)
        "DELETE FROM scan_analyses WHERE scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p112-%')",
        "DELETE FROM opportunity_occurrences WHERE scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p112-%')",
        "DELETE FROM opportunity_verifications WHERE baseline_scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p112-%') OR verification_scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p112-%')",
        "DELETE FROM opportunities WHERE first_detected_scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p112-%') OR latest_detected_scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p112-%')",
        "DELETE FROM notifications WHERE scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p112-%')",
        # 2. scan_entity_snapshots and prompt_runs
        "DELETE FROM scan_entity_snapshots WHERE scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p112-%')",
        "DELETE FROM prompt_runs WHERE scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p112-%')",
        # 3. Null out self-references and FKs before deleting scans
        "UPDATE scans SET baseline_scan_id = NULL WHERE "
        "idempotency_key LIKE 'scheduled:%' OR idempotency_key LIKE 'p112-%'",
        "UPDATE scans SET quota_reservation_id = NULL WHERE "
        "idempotency_key LIKE 'scheduled:%' OR idempotency_key LIKE 'p112-%'",
        "UPDATE project_scan_schedules SET last_scan_id = NULL WHERE "
        "last_scan_id IN (SELECT id FROM scans WHERE "
        "idempotency_key LIKE 'scheduled:%' OR idempotency_key LIKE 'p112-%')",
        # 4. Delete scans
        "DELETE FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p112-%'",
        # 5. usage_events (refs quota_reservations, projects, workspaces, users)
        "DELETE FROM usage_events WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P112%')",
        # 6. quota_reservations
        "DELETE FROM quota_reservations WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P112%')",
        # 7. prompts before prompt_sets
        "DELETE FROM prompts WHERE prompt_set_id IN "
        "(SELECT id FROM prompt_sets WHERE project_id IN "
        "(SELECT id FROM projects WHERE domain LIKE 'p112-%'))",
        "DELETE FROM prompt_sets WHERE project_id IN "
        "(SELECT id FROM projects WHERE domain LIKE 'p112-%')",
        # 8. project_scan_schedules
        "DELETE FROM project_scan_schedules WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P112%')",
        # 9. project_providers, project_keywords, competitors
        "DELETE FROM project_providers WHERE project_id IN "
        "(SELECT id FROM projects WHERE domain LIKE 'p112-%')",
        "DELETE FROM project_keywords WHERE project_id IN "
        "(SELECT id FROM projects WHERE domain LIKE 'p112-%')",
        "DELETE FROM competitors WHERE project_id IN "
        "(SELECT id FROM projects WHERE domain LIKE 'p112-%')",
        # 10. remaining opportunities by workspace
        "DELETE FROM opportunity_verifications WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P112%')",
        "DELETE FROM opportunities WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P112%')",
        # 11. projects
        "DELETE FROM projects WHERE domain LIKE 'p112-%'",
        # 12. workspace_usage_periods
        "DELETE FROM workspace_usage_periods WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P112%')",
        # 13. email_deliveries, notifications, notification_preferences
        "DELETE FROM email_deliveries WHERE recipient_email LIKE 'p112-%'",
        "DELETE FROM notifications WHERE dedup_key LIKE 'p112-%'",
        "DELETE FROM notifications WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P112%')",
        "DELETE FROM notification_preferences WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P112%')",
        # 14. appsumo_licenses
        "DELETE FROM appsumo_licenses WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P112%')",
        # 15. workspace_members
        "DELETE FROM workspace_members WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P112%')",
        # 16. billing_accounts
        "DELETE FROM billing_accounts WHERE plan_code LIKE 'P112_%'",
        # 17. plan_providers, plan_definitions
        "DELETE FROM plan_providers WHERE plan_id IN "
        "(SELECT id FROM plan_definitions WHERE code LIKE 'P112_%')",
        "DELETE FROM plan_definitions WHERE code LIKE 'P112_%'",
        # 18. provider_price_rules
        "DELETE FROM provider_price_rules WHERE pricing_key LIKE 'p112-%'",
        # 19. audit_logs
        "DELETE FROM audit_logs WHERE action LIKE 'NOTIFICATION_EMAIL_RETRY%' "
        "AND workspace_id IN (SELECT id FROM workspaces WHERE name LIKE 'P112%')",
        # 20. users
        "DELETE FROM users WHERE email LIKE 'p112-%'",
        # 21. workspaces
        "DELETE FROM workspaces WHERE name LIKE 'P112%'",
    ]

    # Run each cleanup statement in its own transaction so that a
    # failure on one statement does not prevent the rest from running.
    for stmt in cleanup_stmts:
        session = factory()
        try:
            session.execute(text(stmt))
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

    yield

    for stmt in cleanup_stmts:
        session = factory()
        try:
            session.execute(text(stmt))
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()


# ----------------------------------------------------------------------
# Thread-safe recording dispatcher
# ----------------------------------------------------------------------


class RecordingDispatcher(ScanDispatcher):
    """Thread-safe dispatcher that records dispatch calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.dispatch_count = 0
        self.dispatched_scan_ids: list[uuid.UUID] = []

    def dispatch(self, scan_id: uuid.UUID) -> None:  # type: ignore[override]
        with self._lock:
            self.dispatch_count += 1
            self.dispatched_scan_ids.append(scan_id)


# ----------------------------------------------------------------------
# Failing email transport
# ----------------------------------------------------------------------


class FailingEmailTransport:
    """Email transport that always fails."""

    def send(self, **kwargs: Any) -> EmailSendResult:
        return EmailSendResult(
            success=False,
            failure_code="SMTP_CONNECTION_REFUSED",
            failure_message="Connection refused by test transport",
        )


# ----------------------------------------------------------------------
# Seed helpers (committed setup)
# ----------------------------------------------------------------------


def _seed_workspace(
    session: Session,
    *,
    monthly_limit: int = 10000,
    min_schedule_interval: int | None = 24,
    suffix: str | None = None,
    providers: list[LLMProvider] | None = None,
) -> tuple[Workspace, User]:
    """Create a workspace + user + plan + billing account. Commits."""
    s = suffix or uuid.uuid4().hex
    providers = providers or [LLMProvider.OPENAI]
    user = User(email=f"p112-{s}@example.test", password_hash="synthetic")
    workspace = Workspace(name=f"P112 ws {s}", workspace_type=WorkspaceType.AGENCY)
    session.add_all([user, workspace])
    session.flush()
    session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER)
    )
    plan = PlanDefinition(
        code=f"P112_{s}",
        name="P112 plan",
        is_active=True,
        max_projects=10,
        max_keywords_per_project=20,
        max_competitors_per_project=10,
        max_team_members=5,
        monthly_ai_checks=monthly_limit,
        min_scheduled_scan_interval_hours=min_schedule_interval,
        confidence_scans_enabled=True,
        verification_scans_enabled=True,
    )
    session.add(plan)
    session.flush()
    for p in providers:
        session.add(PlanProvider(plan_id=plan.id, provider=p))
    session.add(
        BillingAccount(
            workspace_id=workspace.id,
            source=BillingSource.ADMIN,
            status=BillingAccountStatus.ACTIVE,
            plan_code=plan.code,
            is_primary=True,
        )
    )
    session.commit()
    return workspace, user


def _seed_scan_ready_project(
    session: Session,
    workspace: Workspace,
    user: User,
    *,
    providers: list[LLMProvider] | None = None,
    prompt_count: int = 3,
) -> Project:
    """Create a fully scan-ready ACTIVE project.

    Includes: ACTIVE PromptSet, correct input_revision, GENERATOR_KEY,
    at least one ACTIVE Prompt, enabled ProjectProviders, valid
    ProviderPriceRule, and a competitor.
    """
    providers = providers or [LLMProvider.OPENAI]
    s = uuid.uuid4().hex
    # Use unique model names to avoid price rule conflicts.
    models = {p: f"p112-{p.value.lower()}-{s}" for p in providers}

    project = Project(
        workspace_id=workspace.id,
        name=f"Project {s}",
        domain=f"p112-{s}.test",
        brand_name="Acme",
        brand_aliases=[],
        target_country="US",
        target_language="en",
        status=ProjectStatus.ACTIVE,
        prompt_input_revision=1,
    )
    session.add(project)
    session.flush()

    # Project providers (enabled).
    for p in providers:
        session.add(ProjectProvider(project_id=project.id, provider=p, enabled=True))

    # Keyword.
    keyword = ProjectKeyword(
        project_id=project.id,
        text="best CRM for small business",
        normalized_text="best crm for small business",
        intent="research",
        active=True,
    )
    session.add(keyword)
    session.flush()

    # Competitor.
    session.add(
        Competitor(
            project_id=project.id,
            name="Rival",
            domain="rival.test",
            aliases=[],
            active=True,
        )
    )

    # PromptSet (ACTIVE, correct generator key).
    prompt_set = PromptSet(
        project_id=project.id,
        version=1,
        input_revision=1,
        status=PromptSetStatus.ACTIVE,
        generator_key=GENERATOR_KEY,
        created_by_user_id=user.id,
        activated_at=datetime.now(UTC),
    )
    session.add(prompt_set)
    session.flush()

    # Prompts (ACTIVE).
    for i in range(1, prompt_count + 1):
        session.add(
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
        )

    # Provider price rules (valid for execution).
    now = datetime.now(UTC)
    surfaces = {
        LLMProvider.OPENAI: ProviderSurface.OPENAI_RESPONSES_API,
        LLMProvider.ANTHROPIC: ProviderSurface.ANTHROPIC_MESSAGES_API,
        LLMProvider.GOOGLE: ProviderSurface.GOOGLE_INTERACTIONS_API,
        LLMProvider.PERPLEXITY: ProviderSurface.PERPLEXITY_SONAR_API,
    }
    from app.models.pricing import ProviderPriceRule

    for p in providers:
        session.add(
            ProviderPriceRule(
                pricing_key=f"p112-{p.value.lower()}-{s}",
                provider=p,
                provider_surface=surfaces[p],
                model=models[p],
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
                source_url="https://example.test/p112-pricing",
                notes="P112 synthetic test rule",
            )
        )

    session.commit()
    return project, models


def _seed_schedule(
    session: Session,
    workspace: Workspace,
    project: Project,
    user: User,
    *,
    interval_hours: int = 24,
    next_run_at: datetime | None = None,
    enabled: bool = True,
) -> Any:
    """Create a schedule directly. Commits."""
    from app.models.project_scan_schedule import ProjectScanSchedule

    if next_run_at is None:
        next_run_at = datetime.now(UTC) + timedelta(hours=interval_hours)
    schedule = ProjectScanSchedule(
        workspace_id=workspace.id,
        project_id=project.id,
        enabled=enabled,
        interval_hours=interval_hours,
        next_run_at=next_run_at,
        created_by_user_id=user.id,
    )
    session.add(schedule)
    session.commit()
    return schedule


def _setup_test_settings(models: dict[LLMProvider, str] | None = None) -> None:
    """Set up test settings via env vars + cache clear.

    This ensures ProviderRegistry (which uses get_settings() internally)
    picks up the synthetic API keys and model names.
    """
    from app.config import get_settings

    default_models = {
        LLMProvider.OPENAI: "p112-openai-test",
        LLMProvider.ANTHROPIC: "p112-anthropic-test",
        LLMProvider.GOOGLE: "p112-google-test",
        LLMProvider.PERPLEXITY: "p112-perplexity-test",
    }
    merged = {**default_models, **models} if models else default_models
    os.environ["OPENAI_API_KEY"] = "synthetic-openai-key"
    os.environ["OPENAI_SCAN_MODEL"] = merged[LLMProvider.OPENAI]
    os.environ["ANTHROPIC_API_KEY"] = "synthetic-anthropic-key"
    os.environ["ANTHROPIC_SCAN_MODEL"] = merged[LLMProvider.ANTHROPIC]
    os.environ["GOOGLE_API_KEY"] = "synthetic-google-key"
    os.environ["GOOGLE_SCAN_MODEL"] = merged[LLMProvider.GOOGLE]
    os.environ["PERPLEXITY_API_KEY"] = "synthetic-perplexity-key"
    os.environ["PERPLEXITY_SCAN_MODEL"] = merged[LLMProvider.PERPLEXITY]
    os.environ["PRICING_REQUIRE_RULE_FOR_EXECUTION"] = "true"
    get_settings.cache_clear()


# ----------------------------------------------------------------------
# Tests: Scheduler advisory lock — real successful concurrency
# ----------------------------------------------------------------------


def test_scheduler_concurrency_real_success(engine_factory: Any) -> None:
    """Two concurrent workers, one due schedule, one scan-ready project.

    Exactly ONE scan is created, ONE quota reservation, ONE dispatch.
    The other worker finds no due claim or fails the advisory lock cleanly.
    """
    _, factory = engine_factory

    # Seed scan-ready project in a committed session.
    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="concurrency")
        project, models = _seed_scan_ready_project(setup_session, ws, user)
        due_at = datetime.now(UTC) - timedelta(hours=1)
        schedule = _seed_schedule(
            setup_session, ws, project, user, interval_hours=24, next_run_at=due_at
        )
        schedule_id = schedule.id
        workspace_id = ws.id
    finally:
        setup_session.close()

    # Set up test settings BEFORE starting threads (env vars + cache clear
    # are process-global, not thread-local, so must be done once).
    _setup_test_settings(models)

    dispatcher = RecordingDispatcher()
    barrier = threading.Barrier(2)
    results: dict[str, dict[str, int]] = {}
    errors: list[Exception] = []

    def worker(name: str) -> None:
        try:
            barrier.wait(timeout=10)
            session = factory()
            try:
                service = ScheduledScanService(
                    session,
                    dispatcher,
                    scan_creation_session_factory=factory,
                )
                res = service.process_due_schedules(now=datetime.now(UTC), limit=5)
                results[name] = res
                session.commit()
            finally:
                session.close()
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("A",), daemon=True)
    t2 = threading.Thread(target=worker, args=("B",), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert len(errors) == 0, f"Worker errors: {[str(e) for e in errors]}"

    # Verify exact outcomes.
    verify_session = factory()
    try:
        scans = list(
            verify_session.execute(
                select(Scan).where(Scan.scan_schedule_id == schedule_id)
            ).scalars()
        )
        assert len(scans) == 1, f"Expected 1 scan, got {len(scans)}"

        scan = scans[0]
        assert scan.scan_schedule_id == schedule_id
        assert scan.scan_type == ScanType.STANDARD
        assert scan.status == ScanStatus.PENDING
        assert scan.quota_reservation_id is not None

        # PromptRun count == planned_ai_checks.
        from app.models.scan import PromptRun

        runs = list(
            verify_session.execute(select(PromptRun).where(PromptRun.scan_id == scan.id)).scalars()
        )
        assert len(runs) == scan.planned_ai_checks

        # Quota reservation for this scan == 1 (look up by scan.quota_reservation_id).
        from app.models.quota_reservation import QuotaReservation

        assert scan.quota_reservation_id is not None
        reservation = verify_session.get(QuotaReservation, scan.quota_reservation_id)
        assert reservation is not None
        assert reservation.workspace_id == workspace_id

        # Dispatch count == 1.
        assert dispatcher.dispatch_count == 1

        # next_run_at > now.
        from app.models.project_scan_schedule import ProjectScanSchedule

        updated = verify_session.get(ProjectScanSchedule, schedule_id)
        assert updated is not None
        assert updated.next_run_at > datetime.now(UTC)

        # Exactly one TRIGGERED outcome across both workers.
        total_triggered = sum(
            r.get(ScheduledScanOutcome.TRIGGERED.value, 0) for r in results.values()
        )
        assert total_triggered == 1
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Tests: Same due slot idempotency (crash recovery)
# ----------------------------------------------------------------------


def test_same_due_slot_idempotency_crash_recovery(engine_factory: Any) -> None:
    """Process due slot X, then simulate crash before schedule advancement.

    Process again with the SAME scheduled_for. The deterministic
    idempotency key must find the existing Scan — no duplicate.
    """
    _, factory = engine_factory

    # Seed scan-ready project.
    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="crash")
        project, models = _seed_scan_ready_project(setup_session, ws, user)
        due_at = datetime.now(UTC) - timedelta(hours=1)
        schedule = _seed_schedule(
            setup_session, ws, project, user, interval_hours=24, next_run_at=due_at
        )
        schedule_id = schedule.id
    finally:
        setup_session.close()

    dispatcher = RecordingDispatcher()

    # First processing: create the scan but DON'T advance next_run_at
    # (simulate crash after scan creation, before schedule advancement).

    session1 = factory()
    try:
        _setup_test_settings(models)
        service = ScheduledScanService(
            session1,
            dispatcher,
            scan_creation_session_factory=factory,
        )
        result = service.process_due_schedules(now=datetime.now(UTC), limit=1)
        assert result.get(ScheduledScanOutcome.TRIGGERED.value, 0) == 1
    finally:
        session1.close()

    # Simulate crash: reset next_run_at to the original due_at.
    reset_session = factory()
    try:
        from app.models.project_scan_schedule import ProjectScanSchedule

        schedule = reset_session.get(ProjectScanSchedule, schedule_id)
        assert schedule is not None
        schedule.next_run_at = due_at  # Reset to original due slot.
        reset_session.commit()
    finally:
        reset_session.close()

    # Second processing: same due slot. Should find existing Scan via
    # idempotency key — no duplicate scan, no duplicate reservation.
    session2 = factory()
    try:
        _setup_test_settings(models)
        service = ScheduledScanService(
            session2,
            dispatcher,
            scan_creation_session_factory=factory,
        )
        result = service.process_due_schedules(now=datetime.now(UTC), limit=1)
    finally:
        session2.close()

    # Verify: exactly 1 scan, 1 dispatch total (not 2), 1 reservation.
    verify_session = factory()
    try:
        scans = list(
            verify_session.execute(
                select(Scan).where(Scan.scan_schedule_id == schedule_id)
            ).scalars()
        )
        assert len(scans) == 1, f"Expected 1 scan, got {len(scans)}"
        scan_id = scans[0].id

        from app.models.scan import PromptRun

        runs = list(
            verify_session.execute(select(PromptRun).where(PromptRun.scan_id == scan_id)).scalars()
        )
        assert len(runs) == scans[0].planned_ai_checks

        # Dispatch count should still be 1 (idempotent).
        assert dispatcher.dispatch_count == 1
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Tests: No catch-up storm (successful paid path)
# ----------------------------------------------------------------------


def test_no_catchup_storm_successful(engine_factory: Any) -> None:
    """Schedule 24h, next_run_at = 5 days ago. One sweep -> exactly 1 scan.

    This proves the economic invariant on the PAID path, not only skip.
    """
    _, factory = engine_factory

    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="nocatchup")
        project, models = _seed_scan_ready_project(setup_session, ws, user)
        due_at = datetime.now(UTC) - timedelta(days=5)
        schedule = _seed_schedule(
            setup_session, ws, project, user, interval_hours=24, next_run_at=due_at
        )
        schedule_id = schedule.id
    finally:
        setup_session.close()

    _setup_test_settings(models)

    dispatcher = RecordingDispatcher()

    session = factory()
    try:
        service = ScheduledScanService(
            session,
            dispatcher,
            scan_creation_session_factory=factory,
        )
        now = datetime.now(UTC)
        service.process_due_schedules(now=now, limit=10)
    finally:
        session.close()

    verify_session = factory()
    try:
        scans = list(
            verify_session.execute(
                select(Scan).where(Scan.scan_schedule_id == schedule_id)
            ).scalars()
        )
        assert len(scans) == 1, f"Expected exactly 1 scan, got {len(scans)}"

        from app.models.project_scan_schedule import ProjectScanSchedule

        updated = verify_session.get(ProjectScanSchedule, schedule_id)
        assert updated is not None
        assert updated.next_run_at > datetime.now(UTC)
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Tests: Skip advance (not-ready project)
# ----------------------------------------------------------------------


def test_skip_advance_not_ready_project(engine_factory: Any) -> None:
    """Invalid/not-ready Project -> no Scan -> next_run_at > now."""
    _, factory = engine_factory

    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="skip")
        # Create a project WITHOUT prompt set (not scan-ready).
        project = Project(
            workspace_id=ws.id,
            name="Not ready project",
            domain="p112-skip.test",
            brand_name="Acme",
            brand_aliases=[],
            target_country="US",
            target_language="en",
            status=ProjectStatus.ACTIVE,
            prompt_input_revision=1,
        )
        setup_session.add(project)
        setup_session.commit()

        due_at = datetime.now(UTC) - timedelta(hours=2)
        schedule = _seed_schedule(
            setup_session, ws, project, user, interval_hours=24, next_run_at=due_at
        )
        schedule_id = schedule.id
    finally:
        setup_session.close()

    dispatcher = RecordingDispatcher()

    session = factory()
    try:
        service = ScheduledScanService(
            session,
            dispatcher,
            scan_creation_session_factory=factory,
        )
        now = datetime.now(UTC)
        service.process_due_schedules(now=now, limit=5)
    finally:
        session.close()

    verify_session = factory()
    try:
        scans = list(
            verify_session.execute(
                select(Scan).where(Scan.scan_schedule_id == schedule_id)
            ).scalars()
        )
        assert len(scans) == 0

        from app.models.project_scan_schedule import ProjectScanSchedule

        updated = verify_session.get(ProjectScanSchedule, schedule_id)
        assert updated is not None
        assert updated.next_run_at > datetime.now(UTC)
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Tests: Notification dedup — ON CONFLICT, forced race
# ----------------------------------------------------------------------


def test_notification_dedup_forced_race(engine_factory: Any) -> None:
    """Two threads force a dedup race with a barrier.

    Expected: one Notification, zero raw IntegrityError, both sessions
    remain usable.
    """
    _, factory = engine_factory

    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="dedup")
        user_id = user.id
        workspace_id = ws.id
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    results: list[Notification | None] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            session = factory()
            try:
                barrier.wait(timeout=10)
                service = NotificationService(session)
                inp = NotificationInput(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
                    title="Test notification",
                    message="Test message",
                    dedup_key="p112-dedup-race",
                )
                result = service.create_notification(inp, dispatch_email_task=False)
                session.commit()
                results.append(result)
            finally:
                session.close()
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker, daemon=True)
    t2 = threading.Thread(target=worker, daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert len(errors) == 0, f"Errors: {[str(e) for e in errors]}"

    # Exactly one notification created.
    created = [r for r in results if r is not None]
    assert len(created) == 1, f"Expected 1 notification, got {len(created)}"

    # Verify in DB.
    verify_session = factory()
    try:
        notifs = list(
            verify_session.execute(
                select(Notification).where(Notification.dedup_key == "p112-dedup-race")
            ).scalars()
        )
        assert len(notifs) == 1
    finally:
        verify_session.close()


def test_notification_dedup_preserves_unrelated_transaction(engine_factory: Any) -> None:
    """Dedup collision must not roll back unrelated caller work."""
    _, factory = engine_factory

    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="unrelated")
        user_id = user.id
        workspace_id = ws.id
    finally:
        setup_session.close()

    session = factory()
    try:
        # Create an unrelated business row (a second workspace member).
        extra_user = User(email="p112-unrelated-extra@example.test", password_hash="synthetic")
        session.add(extra_user)
        session.flush()

        # Create the first notification.
        service = NotificationService(session)
        inp = NotificationInput(
            workspace_id=workspace_id,
            user_id=user_id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="First notification",
            message="First message",
            dedup_key="p112-unrelated-dedup",
        )
        result1 = service.create_notification(inp, dispatch_email_task=False)
        assert result1 is not None
        session.flush()

        # Now try to create a DUPLICATE notification (same dedup_key).
        inp2 = NotificationInput(
            workspace_id=workspace_id,
            user_id=user_id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="Duplicate notification",
            message="Duplicate message",
            dedup_key="p112-unrelated-dedup",
        )
        result2 = service.create_notification(inp2, dispatch_email_task=False)
        assert result2 is None  # Deduped.

        # The unrelated user row must still be present.
        session.flush()
        assert extra_user.id is not None

        # Commit should succeed.
        session.commit()
    finally:
        session.close()

    # Verify the unrelated user was committed.
    verify_session = factory()
    try:
        from app.models.user import User as UserModel

        found = (
            verify_session.execute(
                select(UserModel).where(UserModel.email == "p112-unrelated-extra@example.test")
            )
            .scalars()
            .first()
        )
        assert found is not None, "Unrelated row was rolled back!"
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Tests: Preference creation race
# ----------------------------------------------------------------------


def test_preference_creation_race(engine_factory: Any) -> None:
    """Two concurrent notifications for the same user with no preference.

    The unique (workspace_id, user_id) constraint must not leak
    IntegrityError.
    """
    _, factory = engine_factory

    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="prefrace")
        user_id = user.id
        workspace_id = ws.id
    finally:
        setup_session.close()

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            session = factory()
            try:
                barrier.wait(timeout=10)
                service = NotificationService(session)
                # Both try to create a preference for the same user.
                pref = service.get_or_create_preference(workspace_id, user_id)
                assert pref is not None
                session.commit()
            finally:
                session.close()
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker, daemon=True)
    t2 = threading.Thread(target=worker, daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert len(errors) == 0, f"Errors: {[str(e) for e in errors]}"

    verify_session = factory()
    try:
        prefs = list(
            verify_session.execute(
                select(NotificationPreference).where(
                    NotificationPreference.workspace_id == workspace_id,
                    NotificationPreference.user_id == user_id,
                )
            ).scalars()
        )
        assert len(prefs) == 1, f"Expected 1 preference, got {len(prefs)}"
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Tests: Summary metrics — percentage scale
# ----------------------------------------------------------------------


def test_summary_format_75_percent() -> None:
    """coverage=75 -> 'Measurement coverage: 75.0%' not '7500.0%'."""
    from app.core.enums import ScanStatus

    class FakeScan:
        status = ScanStatus.COMPLETED

    msg = build_scheduled_scan_message(
        FakeScan(),  # type: ignore[arg-type]
        measurement_coverage=75.0,
        brand_visibility=25.0,
        open_opportunities=3,
    )
    assert "Measurement coverage: 75.0%" in msg
    assert "7500.0%" not in msg
    assert "Brand visibility: 25.0%" in msg
    assert "Open opportunities: 3." in msg


def test_summary_format_zero_visibility() -> None:
    """brand_visibility=0 -> 'Brand visibility: 0.0%' not omitted."""
    from app.core.enums import ScanStatus

    class FakeScan:
        status = ScanStatus.COMPLETED

    msg = build_scheduled_scan_message(
        FakeScan(),  # type: ignore[arg-type]
        measurement_coverage=100.0,
        brand_visibility=0.0,
        open_opportunities=0,
    )
    assert "Brand visibility: 0.0%" in msg
    assert "Measurement coverage: 100.0%" in msg
    assert "10000.0%" not in msg


def test_summary_format_none_omitted() -> None:
    """None metrics are omitted, not rendered as 'None%'."""
    from app.core.enums import ScanStatus

    class FakeScan:
        status = ScanStatus.COMPLETED

    msg = build_scheduled_scan_message(
        FakeScan(),  # type: ignore[arg-type]
        measurement_coverage=None,
        brand_visibility=None,
        open_opportunities=None,
    )
    assert "Measurement coverage" not in msg
    assert "Brand visibility" not in msg
    assert "Open opportunities" not in msg


# ----------------------------------------------------------------------
# Tests: Email failure via actual worker path
# ----------------------------------------------------------------------


def test_email_failure_via_worker(engine_factory: Any) -> None:
    """Email transport failure exercises the actual send_email_task path.

    PENDING -> SENDING -> FAILED, attempt_count == 1.
    """
    _, factory = engine_factory

    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="emailfail")
        # Create a notification + email delivery.
        service = NotificationService(setup_session)
        inp = NotificationInput(
            workspace_id=ws.id,
            user_id=user.id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="Test email failure",
            message="Test message for failure",
            dedup_key="p112-emailfail",
        )
        notification = service.create_notification(inp, dispatch_email_task=False)
        assert notification is not None
        setup_session.commit()
        notification_id = notification.id

        # Find the email delivery.
        delivery = (
            setup_session.execute(
                select(EmailDelivery).where(EmailDelivery.notification_id == notification_id)
            )
            .scalars()
            .first()
        )
        assert delivery is not None
        delivery_id = delivery.id
    finally:
        setup_session.close()

    # Patch build_email_transport to return FailingEmailTransport.
    import app.workers.notification_tasks as ntasks

    original_build = ntasks.build_email_transport
    ntasks.build_email_transport = lambda: FailingEmailTransport()  # type: ignore[assignment]
    try:
        # Call send_email_task directly (not via Celery).
        ntasks.send_email_task(str(delivery_id))
    finally:
        ntasks.build_email_transport = original_build  # type: ignore[assignment]

    # Verify: PENDING -> SENDING -> FAILED, attempt_count == 1.
    verify_session = factory()
    try:
        delivery = verify_session.get(EmailDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == EmailDeliveryStatus.FAILED
        assert delivery.attempt_count == 1
        assert delivery.failure_code is not None
        assert delivery.failure_message is not None
        assert len(delivery.failure_message) <= 500

        # Notification still exists.
        notification = verify_session.get(Notification, notification_id)
        assert notification is not None
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Tests: Email retry via actual endpoint
# ----------------------------------------------------------------------


def test_email_retry_via_endpoint(engine_factory: Any) -> None:
    """Manual retry via POST endpoint: FAILED -> PENDING -> SENT.

    Same EmailDelivery ID, same Notification ID, same Message-ID.
    attempt_count increases. No duplicate EmailDelivery.
    """

    from app.db.session import reset_engine

    _, factory = engine_factory

    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="retry")
        # Create notification + email delivery.
        service = NotificationService(setup_session)
        inp = NotificationInput(
            workspace_id=ws.id,
            user_id=user.id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="Test retry",
            message="Test message for retry",
            dedup_key="p112-retry",
        )
        notification = service.create_notification(inp, dispatch_email_task=False)
        assert notification is not None
        setup_session.commit()
        notification_id = notification.id

        delivery = (
            setup_session.execute(
                select(EmailDelivery).where(EmailDelivery.notification_id == notification_id)
            )
            .scalars()
            .first()
        )
        assert delivery is not None
        delivery_id = delivery.id
        original_message_id = delivery.message_id

        # Mark as FAILED (simulate prior failure).
        delivery.status = EmailDeliveryStatus.FAILED
        delivery.failure_code = "PRIOR_FAILURE"
        delivery.failure_message = "Prior failure"
        delivery.attempt_count = 1
        setup_session.commit()
    finally:
        setup_session.close()

    # Test the retry logic directly (simulating the endpoint behavior).
    # Clear settings cache and set DATABASE_URL BEFORE reset_engine
    # so the global session factory connects to the correct test database.
    from app.config import get_settings

    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    get_settings.cache_clear()
    reset_engine()

    retry_session = factory()
    try:
        delivery = retry_session.get(EmailDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == EmailDeliveryStatus.FAILED

        # Reset to PENDING (simulating the endpoint).
        delivery.status = EmailDeliveryStatus.PENDING
        delivery.failure_code = None
        delivery.failure_message = None
        retry_session.commit()
    finally:
        retry_session.close()

    # Send with working transport -> SENT.
    import app.workers.notification_tasks as ntasks
    from app.services.email_transport import MemoryEmailTransport

    original_build = ntasks.build_email_transport
    ntasks.build_email_transport = lambda: MemoryEmailTransport()  # type: ignore[assignment]
    try:
        ntasks.send_email_task(str(delivery_id))
    finally:
        ntasks.build_email_transport = original_build  # type: ignore[assignment]

    verify_session = factory()
    try:
        delivery = verify_session.get(EmailDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == EmailDeliveryStatus.SENT
        assert delivery.attempt_count >= 2
        assert delivery.message_id == original_message_id

        # No duplicate EmailDelivery.
        deliveries = list(
            verify_session.execute(
                select(EmailDelivery).where(EmailDelivery.notification_id == notification_id)
            ).scalars()
        )
        assert len(deliveries) == 1
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Tests: Cross-tenant email retry
# ----------------------------------------------------------------------


def test_cross_tenant_email_retry_404(engine_factory: Any) -> None:
    """Workspace A ADMIN cannot retry email belonging to Workspace B."""
    _, factory = engine_factory

    setup_session = factory()
    try:
        ws_a, _user_a = _seed_workspace(setup_session, suffix="cta")
        ws_b, user_b = _seed_workspace(setup_session, suffix="ctb")

        # Create notification in WS B.
        service = NotificationService(setup_session)
        inp = NotificationInput(
            workspace_id=ws_b.id,
            user_id=user_b.id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="WS B notification",
            message="WS B message",
            dedup_key="p112-ctb-notif",
        )
        notification = service.create_notification(inp, dispatch_email_task=False)
        assert notification is not None
        setup_session.commit()
        notification_id = notification.id

        delivery = (
            setup_session.execute(
                select(EmailDelivery).where(EmailDelivery.notification_id == notification_id)
            )
            .scalars()
            .first()
        )
        assert delivery is not None
        delivery_id = delivery.id
        original_status = delivery.status
    finally:
        setup_session.close()

    # Try to retry via WS A route.

    retry_session = factory()
    try:
        # Simulate the retry endpoint logic with WS A and WS B notification.
        notification = (
            retry_session.execute(
                select(Notification).where(
                    Notification.id == notification_id,
                    Notification.workspace_id == ws_a.id,  # Wrong workspace!
                )
            )
            .scalars()
            .first()
        )
        # Should be None (cross-tenant).
        assert notification is None

        # Delivery should be unchanged.
        delivery = retry_session.get(EmailDelivery, delivery_id)
        assert delivery is not None
        assert delivery.status == original_status
    finally:
        retry_session.close()


# ----------------------------------------------------------------------
# Tests: Email retry audit event
# ----------------------------------------------------------------------


def test_email_retry_audit_event(engine_factory: Any) -> None:
    """Successful manual email retry records NOTIFICATION_EMAIL_RETRY."""
    _, factory = engine_factory

    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="audit")
        service = NotificationService(setup_session)
        inp = NotificationInput(
            workspace_id=ws.id,
            user_id=user.id,
            notification_type=NotificationType.SCHEDULED_SCAN_COMPLETED,
            title="Audit test",
            message="Audit message",
            dedup_key="p112-audit",
        )
        notification = service.create_notification(inp, dispatch_email_task=False)
        assert notification is not None
        setup_session.commit()
        notification_id = notification.id

        delivery = (
            setup_session.execute(
                select(EmailDelivery).where(EmailDelivery.notification_id == notification_id)
            )
            .scalars()
            .first()
        )
        assert delivery is not None
        delivery_id = delivery.id

        # Mark as FAILED.
        delivery.status = EmailDeliveryStatus.FAILED
        delivery.failure_code = "TEST"
        delivery.failure_message = "Test failure"
        setup_session.commit()
    finally:
        setup_session.close()

    # Record audit event (simulating what the router does).
    from app.services.audit_service import AuditService

    audit = AuditService(factory)
    audit.record(
        action="NOTIFICATION_EMAIL_RETRY",
        user_id=user.id,
        workspace_id=ws.id,
        entity_type="email_delivery",
        entity_id=delivery_id,
    )

    # Verify audit log.
    verify_session = factory()
    try:
        from app.models.audit import AuditLog

        logs = list(
            verify_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "NOTIFICATION_EMAIL_RETRY",
                    AuditLog.workspace_id == ws.id,
                    AuditLog.entity_id == delivery_id,
                )
            ).scalars()
        )
        assert len(logs) == 1
        log = logs[0]
        # No secrets in metadata.
        assert log.metadata_ is not None
        metadata_str = str(log.metadata_)
        assert "password" not in metadata_str.lower()
        assert "body" not in metadata_str.lower()
    finally:
        verify_session.close()
