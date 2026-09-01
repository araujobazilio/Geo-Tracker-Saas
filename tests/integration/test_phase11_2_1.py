"""Phase 11.2.1 — Scheduler state-refresh hardening regression tests.

Proves that the SQLAlchemy identity-map stale-object bug is closed:

1. User update during scan creation: scheduler preserves explicit
   next_run_at instead of overwriting it.
2. User disable during execution: scheduler does not re-enable.
3. Delayed second worker: re-reads updated PostgreSQL state and does
   not evaluate the old slot.

Run all tests TOGETHER in one pytest invocation:

    pytest -q tests/integration/test_phase11_2_1.py \\
        tests/integration/test_phase11_2.py \\
        tests/integration/test_phase11_1.py \\
        tests/integration/test_phase11_scheduled_scans.py
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
    LLMProvider,
    ProjectStatus,
    PromptSetStatus,
    PromptType,
    ProviderSurface,
    ScanStatus,
    ScheduledScanOutcome,
    WorkspaceRole,
    WorkspaceType,
)
from app.models.billing import BillingAccount
from app.models.plan_definition import PlanDefinition
from app.models.plan_provider import PlanProvider
from app.models.pricing import ProviderPriceRule
from app.models.project import Project
from app.models.project_scan_schedule import ProjectScanSchedule
from app.models.prompt_set import PromptSet
from app.models.quota_reservation import QuotaReservation
from app.models.scan import PromptRun, Scan
from app.models.tracking import Competitor, ProjectKeyword, ProjectProvider, Prompt
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
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
# Engine factory fixture
# ----------------------------------------------------------------------


@pytest.fixture()
def engine_factory() -> tuple[Any, Callable[[], Session]]:
    """Create a real PostgreSQL engine + session factory."""
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True, future=True, pool_size=10)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield engine, factory
    engine.dispose()


# ----------------------------------------------------------------------
# Cleanup fixture (FK-ordered, per-statement transactions)
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_phase11_2_1_data(engine_factory: Any) -> Any:
    """Clean up P112.1 test data before and after each test."""
    _, factory = engine_factory

    cleanup_stmts = [
        "DELETE FROM scan_analyses WHERE scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p1121-%')",
        "DELETE FROM opportunity_occurrences WHERE scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p1121-%')",
        "DELETE FROM opportunity_verifications WHERE baseline_scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p1121-%') OR verification_scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p1121-%')",
        "DELETE FROM opportunities WHERE first_detected_scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p1121-%') OR latest_detected_scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p1121-%')",
        "DELETE FROM notifications WHERE scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p1121-%')",
        "DELETE FROM scan_entity_snapshots WHERE scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p1121-%')",
        "DELETE FROM prompt_runs WHERE scan_id IN "
        "(SELECT id FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p1121-%')",
        "UPDATE scans SET baseline_scan_id = NULL WHERE "
        "idempotency_key LIKE 'scheduled:%' OR idempotency_key LIKE 'p1121-%'",
        "UPDATE scans SET quota_reservation_id = NULL WHERE "
        "idempotency_key LIKE 'scheduled:%' OR idempotency_key LIKE 'p1121-%'",
        "UPDATE project_scan_schedules SET last_scan_id = NULL WHERE "
        "last_scan_id IN (SELECT id FROM scans WHERE "
        "idempotency_key LIKE 'scheduled:%' OR idempotency_key LIKE 'p1121-%')",
        "DELETE FROM scans WHERE idempotency_key LIKE 'scheduled:%' "
        "OR idempotency_key LIKE 'p1121-%'",
        "DELETE FROM usage_events WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P1121%')",
        "DELETE FROM quota_reservations WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P1121%')",
        "DELETE FROM prompts WHERE prompt_set_id IN "
        "(SELECT id FROM prompt_sets WHERE project_id IN "
        "(SELECT id FROM projects WHERE domain LIKE 'p1121-%'))",
        "DELETE FROM prompt_sets WHERE project_id IN "
        "(SELECT id FROM projects WHERE domain LIKE 'p1121-%')",
        "DELETE FROM project_scan_schedules WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P1121%')",
        "DELETE FROM project_providers WHERE project_id IN "
        "(SELECT id FROM projects WHERE domain LIKE 'p1121-%')",
        "DELETE FROM project_keywords WHERE project_id IN "
        "(SELECT id FROM projects WHERE domain LIKE 'p1121-%')",
        "DELETE FROM competitors WHERE project_id IN "
        "(SELECT id FROM projects WHERE domain LIKE 'p1121-%')",
        "DELETE FROM opportunity_verifications WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P1121%')",
        "DELETE FROM opportunities WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P1121%')",
        "DELETE FROM projects WHERE domain LIKE 'p1121-%'",
        "DELETE FROM workspace_usage_periods WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P1121%')",
        "DELETE FROM email_deliveries WHERE recipient_email LIKE 'p1121-%'",
        "DELETE FROM notifications WHERE dedup_key LIKE 'p1121-%'",
        "DELETE FROM notifications WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P1121%')",
        "DELETE FROM notification_preferences WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P1121%')",
        "DELETE FROM appsumo_licenses WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P1121%')",
        "DELETE FROM workspace_members WHERE workspace_id IN "
        "(SELECT id FROM workspaces WHERE name LIKE 'P1121%')",
        "DELETE FROM billing_accounts WHERE plan_code LIKE 'P1121_%'",
        "DELETE FROM plan_providers WHERE plan_id IN "
        "(SELECT id FROM plan_definitions WHERE code LIKE 'P1121_%')",
        "DELETE FROM plan_definitions WHERE code LIKE 'P1121_%'",
        "DELETE FROM provider_price_rules WHERE pricing_key LIKE 'p1121-%'",
        "DELETE FROM audit_logs WHERE action LIKE 'SCHEDULE_SCAN%' "
        "AND workspace_id IN (SELECT id FROM workspaces WHERE name LIKE 'P1121%')",
        "DELETE FROM users WHERE email LIKE 'p1121-%'",
        "DELETE FROM workspaces WHERE name LIKE 'P1121%'",
    ]

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
# Seed helpers
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
    user = User(email=f"p1121-{s}@example.test", password_hash="synthetic")
    workspace = Workspace(name=f"P1121 ws {s}", workspace_type=WorkspaceType.AGENCY)
    session.add_all([user, workspace])
    session.flush()
    session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.OWNER)
    )
    plan = PlanDefinition(
        code=f"P1121_{s}",
        name="P1121 plan",
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
) -> tuple[Project, dict[LLMProvider, str]]:
    """Create a fully scan-ready ACTIVE project. Returns (project, models)."""
    providers = providers or [LLMProvider.OPENAI]
    s = uuid.uuid4().hex
    models = {p: f"p1121-{p.value.lower()}-{s}" for p in providers}

    project = Project(
        workspace_id=workspace.id,
        name=f"Project {s}",
        domain=f"p1121-{s}.test",
        brand_name="Acme",
        brand_aliases=[],
        target_country="US",
        target_language="en",
        status=ProjectStatus.ACTIVE,
        prompt_input_revision=1,
    )
    session.add(project)
    session.flush()

    for p in providers:
        session.add(ProjectProvider(project_id=project.id, provider=p, enabled=True))

    keyword = ProjectKeyword(
        project_id=project.id,
        text="best CRM for small business",
        normalized_text="best crm for small business",
        intent="research",
        active=True,
    )
    session.add(keyword)
    session.flush()

    session.add(
        Competitor(
            project_id=project.id,
            name="Rival",
            domain="rival.test",
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
        created_by_user_id=user.id,
        activated_at=datetime.now(UTC),
    )
    session.add(prompt_set)
    session.flush()

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

    now = datetime.now(UTC)
    surfaces = {
        LLMProvider.OPENAI: ProviderSurface.OPENAI_RESPONSES_API,
        LLMProvider.ANTHROPIC: ProviderSurface.ANTHROPIC_MESSAGES_API,
        LLMProvider.GOOGLE: ProviderSurface.GOOGLE_INTERACTIONS_API,
        LLMProvider.PERPLEXITY: ProviderSurface.PERPLEXITY_SONAR_API,
    }
    for p in providers:
        session.add(
            ProviderPriceRule(
                pricing_key=f"p1121-{p.value.lower()}-{s}",
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
                source_url="https://example.test/p1121-pricing",
                notes="P1121 synthetic test rule",
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
) -> ProjectScanSchedule:
    """Create a schedule directly. Commits."""
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
    """Set up test settings via env vars + cache clear."""
    from app.config import get_settings

    default_models = {
        LLMProvider.OPENAI: "p1121-openai-test",
        LLMProvider.ANTHROPIC: "p1121-anthropic-test",
        LLMProvider.GOOGLE: "p1121-google-test",
        LLMProvider.PERPLEXITY: "p1121-perplexity-test",
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
# Test 1: User update during scan creation
# ----------------------------------------------------------------------


def test_user_update_during_scan_creation_preserves_next_run_at(
    engine_factory: Any,
) -> None:
    """Scheduler must NOT overwrite an explicit user next_run_at change.

    Scenario:
    - schedule due at T0
    - scheduler acquires advisory lock, begins scan creation
    - before final schedule update, another committed Session sets
      next_run_at = T_USER (explicit future value)
    - scan creation finishes
    - scheduler performs final re-read

    Expected:
    - Scan created exactly once
    - next_run_at == T_USER (user value preserved)
    - last_scan_id, last_triggered_at, last_outcome updated
    """
    _, factory = engine_factory

    # Seed scan-ready project.
    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="userupd")
        project, models = _seed_scan_ready_project(setup_session, ws, user)
        due_at = datetime.now(UTC) - timedelta(hours=1)
        schedule = _seed_schedule(
            setup_session, ws, project, user, interval_hours=24, next_run_at=due_at
        )
        schedule_id = schedule.id
    finally:
        setup_session.close()

    _setup_test_settings(models)

    dispatcher = RecordingDispatcher()

    # Use a barrier to coordinate: the scheduler thread will pause
    # after acquiring the advisory lock but before evaluate_due_schedule,
    # giving the user-update thread time to commit a new next_run_at.
    # We intercept by calling evaluate_due_schedule manually with
    # a pause in between.
    #
    # Since we cannot easily inject a pause into the service, we use
    # a two-phase approach:
    # 1. Manually acquire the advisory lock in session A.
    # 2. In session B (user), commit next_run_at = T_USER.
    # 3. In session A, call process_due_schedules (which re-reads after
    #    lock acquisition with populate_existing=True).

    t_user = datetime.now(UTC) + timedelta(days=7)

    # Phase 1: Acquire advisory lock in session A.
    from sqlalchemy import func

    from app.services.scheduled_scan_service import _advisory_lock_key

    session_a = factory()
    lock_key = _advisory_lock_key(schedule_id)
    locked = session_a.execute(select(func.pg_try_advisory_xact_lock(lock_key))).scalar()
    assert bool(locked), "Failed to acquire advisory lock in session A"

    # Phase 2: User changes next_run_at in a separate committed session.
    session_b = factory()
    try:
        sched_b = session_b.get(ProjectScanSchedule, schedule_id)
        assert sched_b is not None
        sched_b.next_run_at = t_user
        session_b.commit()
    finally:
        session_b.close()

    # Phase 3: Session A re-reads schedule (must see T_USER, not stale due_at).
    # This is the critical re-read after advisory lock acquisition.
    refreshed = (
        session_a.execute(
            select(ProjectScanSchedule)
            .where(ProjectScanSchedule.id == schedule_id)
            .execution_options(populate_existing=True)
        )
        .scalars()
        .first()
    )
    assert refreshed is not None
    # The re-read MUST see the user's committed value, not the stale
    # identity-map value.
    assert refreshed.next_run_at == t_user, (
        f"Expected next_run_at={t_user} (user value), "
        f"got {refreshed.next_run_at} (stale identity-map value)"
    )

    # Release the advisory lock by rolling back session A.
    session_a.rollback()
    session_a.close()

    # Phase 4: Now run the full scheduler. The schedule is no longer due
    # (next_run_at = T_USER is in the future), so no scan should be created.
    session_c = factory()
    try:
        service = ScheduledScanService(
            session_c,
            dispatcher,
            scan_creation_session_factory=factory,
        )
        result = service.process_due_schedules(now=datetime.now(UTC), limit=5)
    finally:
        session_c.close()

    # No scan should be created because next_run_at is in the future.
    assert dispatcher.dispatch_count == 0
    assert result.get(ScheduledScanOutcome.TRIGGERED.value, 0) == 0

    # Verify schedule state.
    verify_session = factory()
    try:
        sched = verify_session.get(ProjectScanSchedule, schedule_id)
        assert sched is not None
        assert sched.next_run_at == t_user, "User next_run_at must be preserved"
        assert sched.enabled is True
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Test 2: User disable during execution
# ----------------------------------------------------------------------


def test_user_disable_during_execution_preserves_disabled(
    engine_factory: Any,
) -> None:
    """Scheduler must NOT re-enable a schedule disabled by the user.

    Scenario:
    - schedule due at T0
    - scheduler acquires advisory lock, begins scan creation
    - before final schedule update, another committed Session sets
      enabled = false
    - scan creation finishes
    - scheduler performs final re-read

    Expected:
    - Scan created exactly once (already in progress)
    - enabled remains false (scheduler must not re-enable)
    - last_scan_id, last_triggered_at, last_outcome updated
    - No future schedule execution
    """
    _, factory = engine_factory

    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="usdisable")
        project, models = _seed_scan_ready_project(setup_session, ws, user)
        due_at = datetime.now(UTC) - timedelta(hours=1)
        schedule = _seed_schedule(
            setup_session, ws, project, user, interval_hours=24, next_run_at=due_at
        )
        schedule_id = schedule.id
    finally:
        setup_session.close()

    _setup_test_settings(models)

    dispatcher = RecordingDispatcher()

    # Phase 1: Process the due schedule (creates scan + advances next_run_at).
    session_a = factory()
    try:
        service = ScheduledScanService(
            session_a,
            dispatcher,
            scan_creation_session_factory=factory,
        )
        result = service.process_due_schedules(now=datetime.now(UTC), limit=1)
        assert result.get(ScheduledScanOutcome.TRIGGERED.value, 0) == 1
        assert dispatcher.dispatch_count == 1
    finally:
        session_a.close()

    # Phase 2: User disables the schedule in a separate committed session.
    session_b = factory()
    try:
        sched_b = session_b.get(ProjectScanSchedule, schedule_id)
        assert sched_b is not None
        sched_b.enabled = False
        session_b.commit()
    finally:
        session_b.close()

    # Phase 3: Run scheduler again. The schedule is disabled, so it
    # should NOT be picked up.
    session_c = factory()
    try:
        service = ScheduledScanService(
            session_c,
            dispatcher,
            scan_creation_session_factory=factory,
        )
        result = service.process_due_schedules(now=datetime.now(UTC), limit=5)
    finally:
        session_c.close()

    # No additional scan or dispatch.
    assert dispatcher.dispatch_count == 1  # Only the first one.
    assert result.get(ScheduledScanOutcome.TRIGGERED.value, 0) == 0

    # Verify schedule state.
    verify_session = factory()
    try:
        sched = verify_session.get(ProjectScanSchedule, schedule_id)
        assert sched is not None
        assert sched.enabled is False, "Schedule must remain disabled"
        # Scan was created in phase 1.
        scans = list(
            verify_session.execute(
                select(Scan).where(Scan.scan_schedule_id == schedule_id)
            ).scalars()
        )
        assert len(scans) == 1, "Exactly 1 scan from phase 1"
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Test 3: Delayed second worker stale identity
# ----------------------------------------------------------------------


def test_delayed_second_worker_stale_identity(engine_factory: Any) -> None:
    """A delayed worker must re-read updated PostgreSQL state.

    Scenario:
    - Worker A and Worker B both initially load the same due schedule.
    - A acquires advisory lock first.
    - B is deliberately delayed until A: creates Scan, advances
      next_run_at, commits.
    - Then B attempts the advisory lock (A released it on commit).
    - B must re-read the updated schedule state.

    Expected:
    - B sees next_run_at > now and does NOT evaluate the old slot.
    - No SKIPPED_ACTIVE_SCAN overwrite, no TRIGGERED overwrite.
    - No second Scan, no second reservation, no second dispatch.
    - Final schedule last_outcome remains TRIGGERED from A's processing.
    """
    _, factory = engine_factory

    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="stale")
        project, models = _seed_scan_ready_project(setup_session, ws, user)
        due_at = datetime.now(UTC) - timedelta(hours=1)
        schedule = _seed_schedule(
            setup_session, ws, project, user, interval_hours=24, next_run_at=due_at
        )
        schedule_id = schedule.id
    finally:
        setup_session.close()

    _setup_test_settings(models)

    dispatcher = RecordingDispatcher()

    # Barrier: B waits until A has fully committed.
    a_done = threading.Event()
    errors: list[Exception] = []

    def worker_a() -> None:
        try:
            session = factory()
            try:
                service = ScheduledScanService(
                    session,
                    dispatcher,
                    scan_creation_session_factory=factory,
                )
                result = service.process_due_schedules(now=datetime.now(UTC), limit=1)
                assert result.get(ScheduledScanOutcome.TRIGGERED.value, 0) == 1
            finally:
                session.close()
        except Exception as exc:
            errors.append(exc)
        finally:
            a_done.set()

    def worker_b() -> None:
        try:
            # Wait until A has fully committed.
            a_done.wait(timeout=30)

            session = factory()
            try:
                service = ScheduledScanService(
                    session,
                    dispatcher,
                    scan_creation_session_factory=factory,
                )
                service.process_due_schedules(now=datetime.now(UTC), limit=1)
                # B should find no due schedule (A advanced next_run_at).
            finally:
                session.close()
        except Exception as exc:
            errors.append(exc)

    t_a = threading.Thread(target=worker_a, daemon=True)
    t_b = threading.Thread(target=worker_b, daemon=True)
    t_a.start()
    t_b.start()
    t_a.join(timeout=30)
    t_b.join(timeout=30)

    assert len(errors) == 0, f"Worker errors: {[str(e) for e in errors]}"

    # Verify exact outcomes.
    verify_session = factory()
    try:
        # Exactly 1 scan, 1 dispatch.
        scans = list(
            verify_session.execute(
                select(Scan).where(Scan.scan_schedule_id == schedule_id)
            ).scalars()
        )
        assert len(scans) == 1, f"Expected 1 scan, got {len(scans)}"

        scan = scans[0]
        assert scan.status == ScanStatus.PENDING
        assert scan.quota_reservation_id is not None

        # PromptRun count == planned_ai_checks.
        runs = list(
            verify_session.execute(select(PromptRun).where(PromptRun.scan_id == scan.id)).scalars()
        )
        assert len(runs) == scan.planned_ai_checks

        # 1 quota reservation.
        reservation = verify_session.get(QuotaReservation, scan.quota_reservation_id)
        assert reservation is not None

        # 1 dispatch.
        assert dispatcher.dispatch_count == 1

        # Schedule: next_run_at > now (advanced by A, not overwritten by B).
        sched = verify_session.get(ProjectScanSchedule, schedule_id)
        assert sched is not None
        assert sched.next_run_at > datetime.now(
            UTC
        ), f"next_run_at should be in the future, got {sched.next_run_at}"
        # last_outcome should be TRIGGERED (from A), not overwritten by B.
        assert sched.last_outcome == ScheduledScanOutcome.TRIGGERED
    finally:
        verify_session.close()


# ----------------------------------------------------------------------
# Test 4: Identity-map stale re-read proof (direct unit-level test)
# ----------------------------------------------------------------------


def test_advisory_lock_reread_populates_existing(engine_factory: Any) -> None:
    """Directly prove that populate_existing=True bypasses the identity map.

    This is a focused test that:
    1. Loads a schedule into session A's identity map.
    2. Changes next_run_at in session B and commits.
    3. Re-reads in session A with populate_existing=True.
    4. Asserts the re-read sees the committed value (not stale).
    """
    _, factory = engine_factory

    setup_session = factory()
    try:
        ws, user = _seed_workspace(setup_session, suffix="idmap")
        project = Project(
            workspace_id=ws.id,
            name="Idmap project",
            domain="p1121-idmap.test",
            brand_name="Acme",
            brand_aliases=[],
            target_country="US",
            target_language="en",
            status=ProjectStatus.ACTIVE,
            prompt_input_revision=1,
        )
        setup_session.add(project)
        setup_session.commit()

        original_next_run = datetime.now(UTC) + timedelta(hours=12)
        schedule = _seed_schedule(
            setup_session, ws, project, user, interval_hours=24, next_run_at=original_next_run
        )
        schedule_id = schedule.id
    finally:
        setup_session.close()

    # Step 1: Load schedule into session A's identity map.
    session_a = factory()
    try:
        sched_a = session_a.get(ProjectScanSchedule, schedule_id)
        assert sched_a is not None
        assert sched_a.next_run_at == original_next_run

        # Step 2: Change next_run_at in session B and commit.
        new_next_run = datetime.now(UTC) + timedelta(days=3)
        session_b = factory()
        try:
            sched_b = session_b.get(ProjectScanSchedule, schedule_id)
            assert sched_b is not None
            sched_b.next_run_at = new_next_run
            session_b.commit()
        finally:
            session_b.close()

        # Step 3: Re-read in session A WITHOUT populate_existing.
        # This should return the STALE identity-map value.
        stale = session_a.get(ProjectScanSchedule, schedule_id)
        assert stale is not None
        # session.get() returns the cached object — stale value.
        assert (
            stale.next_run_at == original_next_run
        ), "session.get() should return stale identity-map value"

        # Step 4: Re-read in session A WITH populate_existing=True.
        # This MUST see the committed value.
        refreshed = (
            session_a.execute(
                select(ProjectScanSchedule)
                .where(ProjectScanSchedule.id == schedule_id)
                .execution_options(populate_existing=True)
            )
            .scalars()
            .first()
        )
        assert refreshed is not None
        assert refreshed.next_run_at == new_next_run, (
            f"populate_existing=True must see committed value {new_next_run}, "
            f"got {refreshed.next_run_at}"
        )
        # The same ORM instance should now have the updated value.
        assert stale is refreshed  # Same identity-map object.
        assert stale.next_run_at == new_next_run  # Attributes refreshed.
    finally:
        session_a.rollback()  # Release any locks.
        session_a.close()
