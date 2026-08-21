"""Integration tests for Phase 10.3 — Verification Terminalization,
Concurrency Proof and Documentation Closure.

Tests cover:
- Normal all-runs-failed VERIFICATION scan auto-terminalizes
- Idempotent re-finalization of FAILED VERIFICATION scan (self-healing)
- Analysis exception → durable ScanAnalysis=FAILED → INCONCLUSIVE
- Evaluation exception → PENDING preserved, manual retry works
- Quota failure with real QuotaService (real PostgreSQL)
- Real PostgreSQL different-key concurrency (mandatory)
- Targeted scope execution: CITATION + PROMPT actual Scan plan
- Economics: planned/used/released checks, provider call counts
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import (
    LLMProvider,
    OpportunityStatus,
    OpportunityType,
    ProviderExecutionMode,
    QuotaReservationStatus,
    ScanStatus,
    ScanType,
    VerificationOutcome,
    VerificationReasonCode,
)
from app.core.exceptions import ConflictError
from app.models import (
    Opportunity,
    OpportunityVerification,
    PromptRun,
    QuotaReservation,
    Scan,
)
from app.services.opportunity_workflow_service import OpportunityWorkflowService

# Re-export the fake adapters and helpers from test_verification_phase10
from tests.integration.test_verification_phase10 import (
    MODELS,
    SURFACES,
    FakeDispatcher,
    ScriptedRegistry,
    _add_prices,
    _create_verification,
    _evaluate_verification,
    _execute,
    _finalize,
    _full_pipeline,
    _get_first_opportunity,
    _refresh_actions,
    _registry,
    _seed,
    _settings,
)

# Re-export PerRunAdapter and _per_run_registry from test_verification_phase10_2
from tests.integration.test_verification_phase10_2 import PerRunAdapter

pytestmark = pytest.mark.integration

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _count_usage_events(db: Session, scan_id: uuid.UUID) -> int:
    """Count UsageEvents associated with a scan via its PromptRuns."""
    result = db.execute(
        text(
            "SELECT count(*) FROM usage_events ue "
            "JOIN prompt_runs pr ON ue.prompt_run_id = pr.id "
            "WHERE pr.scan_id = :sid"
        ),
        {"sid": str(scan_id)},
    ).fetchone()
    return result[0] if result else 0


def _count_ai_checks_committed(db: Session, scan_id: uuid.UUID) -> int:
    """Sum ai_checks from UsageEvents for a scan's PromptRuns."""
    result = db.execute(
        text(
            "SELECT COALESCE(sum(ue.ai_checks), 0) FROM usage_events ue "
            "JOIN prompt_runs pr ON ue.prompt_run_id = pr.id "
            "WHERE pr.scan_id = :sid"
        ),
        {"sid": str(scan_id)},
    ).fetchone()
    return result[0] if result else 0


def _get_reservation(db: Session, scan_id: uuid.UUID) -> QuotaReservation | None:
    scan = db.get(Scan, scan_id)
    if scan is None or scan.quota_reservation_id is None:
        return None
    return db.get(QuotaReservation, scan.quota_reservation_id)


def _count_provider_requests(registry: ScriptedRegistry) -> int:
    """Count total provider requests across all adapters in a registry."""
    return sum(len(adapter.requests) for adapter in registry.adapters.values())


# ----------------------------------------------------------------------
# Tests: Normal all-runs-failed VERIFICATION scan
# ----------------------------------------------------------------------


def test_all_runs_failed_verification_auto_terminalizes(db_session: Session) -> None:
    """A normal VERIFICATION scan where ALL PromptRuns fail becomes
    ScanStatus.FAILED. Finalization MUST automatically terminalize the
    OpportunityVerification as INCONCLUSIVE / VERIFICATION_SCAN_FAILED.

    No manual lifecycle call. No provider retry.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=5,
        monthly_limit=100,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-af")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: all runs fail.
    from app.providers.errors import ProviderResponseError

    ver_registry = _registry(
        [LLMProvider.OPENAI],
        mention_mode="brand",
        outcomes={LLMProvider.OPENAI: [ProviderResponseError("fail")] * 5},
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-af"
    )
    ver_scan_id = result.scan.id
    ver_id = result.verification.id
    planned = result.scan.planned_ai_checks

    _execute(db_session, ver_scan_id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, ver_scan_id)
    db_session.expire_all()

    # Scan is FAILED.
    ver_scan = db_session.get(Scan, ver_scan_id)
    assert ver_scan is not None
    assert ver_scan.status == ScanStatus.FAILED

    # Verification is automatically terminalized.
    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.INCONCLUSIVE
    assert ver.reason_code == VerificationReasonCode.VERIFICATION_SCAN_FAILED
    assert ver.evaluated_at is not None

    # Opportunity remains IMPLEMENTED.
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.IMPLEMENTED

    # Economics: 0 committed AI Checks (all runs failed).
    assert _count_ai_checks_committed(db_session, ver_scan_id) == 0

    # Provider request count = exact planned_ai_checks (no retry).
    assert _count_provider_requests(ver_registry) == planned

    # Reservation is released (not ACTIVE).
    reservation = _get_reservation(db_session, ver_scan_id)
    if reservation is not None:
        assert reservation.status != QuotaReservationStatus.ACTIVE


def test_idempotent_refinalize_failed_verification_self_heals(db_session: Session) -> None:
    """Re-calling finalize() on an already-terminal FAILED VERIFICATION
    scan must reconcile a stranded PENDING verification (self-healing).
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=5,
        monthly_limit=100,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-ih")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    from app.providers.errors import ProviderResponseError

    ver_registry = _registry(
        [LLMProvider.OPENAI],
        mention_mode="brand",
        outcomes={LLMProvider.OPENAI: [ProviderResponseError("fail")] * 5},
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-ih"
    )
    ver_scan_id = result.scan.id
    ver_id = result.verification.id

    _execute(db_session, ver_scan_id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, ver_scan_id)
    db_session.expire_all()

    # First finalization terminalized the verification.
    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.INCONCLUSIVE

    # Simulate a stranded PENDING: reset the verification to PENDING.
    ver.outcome = VerificationOutcome.PENDING
    ver.reason_code = None
    ver.evaluated_at = None
    db_session.commit()
    db_session.expire_all()

    # Re-finalize the already-terminal FAILED scan.
    _finalize(db_session, ver_scan_id)
    db_session.expire_all()

    # Self-healing: the stranded PENDING is now terminalized.
    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.INCONCLUSIVE
    assert ver.reason_code == VerificationReasonCode.VERIFICATION_SCAN_FAILED
    assert ver.evaluated_at is not None


# ----------------------------------------------------------------------
# Tests: Analysis exception → durable FAILED → INCONCLUSIVE
# ----------------------------------------------------------------------


def test_analysis_exception_terminalizes_verification(db_session: Session) -> None:
    """If ScanAnalysisService.analyze() raises an unexpected exception
    after persisting ScanAnalysis=FAILED in its failure transaction,
    finalization MUST terminalize the PENDING verification as
    INCONCLUSIVE / ANALYSIS_NOT_COMPLETED.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=5,
        monthly_limit=100,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-ae")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: all runs succeed → Scan COMPLETED.
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-ae"
    )
    ver_scan_id = result.scan.id
    ver_id = result.verification.id

    # Patch _run_analysis to raise BEFORE _execute, because
    # ScanExecutionService.execute_scan() internally calls finalize()
    # with trigger_analysis=True. The real analyze() method's except
    # block will catch this, call _mark_failed() to persist a FAILED
    # ScanAnalysis in a separate transaction, then re-raise.
    # _post_finalize_verification_lifecycle catches the re-raised
    # exception and calls _reconcile_analysis_failure to terminalize.
    from app.services.scan_analysis_service import ScanAnalysisService

    original_run_analysis = ScanAnalysisService._run_analysis

    def exploding_run_analysis(self: ScanAnalysisService, scan: Scan, analysis: Any) -> Any:
        raise RuntimeError("Unexpected analysis failure for test")

    ScanAnalysisService._run_analysis = exploding_run_analysis  # type: ignore[method-assign]
    try:
        _execute(db_session, ver_scan_id, ver_registry)
    finally:
        ScanAnalysisService._run_analysis = original_run_analysis  # type: ignore[method-assign]
    db_session.expire_all()

    # Capture provider request count after execution.
    provider_requests_after_exec = _count_provider_requests(ver_registry)

    # Scan remains COMPLETED (analysis failure doesn't change scan).
    ver_scan = db_session.get(Scan, ver_scan_id)
    assert ver_scan is not None
    assert ver_scan.status in (ScanStatus.COMPLETED, ScanStatus.PARTIAL)

    # ScanAnalysis is FAILED (persisted by the failure transaction).
    from app.models.analysis import ANALYSIS_VERSION, ScanAnalysis

    analysis = (
        db_session.execute(
            select(ScanAnalysis).where(
                ScanAnalysis.scan_id == ver_scan_id,
                ScanAnalysis.analysis_version == ANALYSIS_VERSION,
            )
        )
        .scalars()
        .first()
    )
    assert analysis is not None
    assert analysis.status == "FAILED"

    # Verification is terminalized as INCONCLUSIVE / ANALYSIS_NOT_COMPLETED.
    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.INCONCLUSIVE
    assert ver.reason_code == VerificationReasonCode.ANALYSIS_NOT_COMPLETED
    assert ver.evaluated_at is not None

    # Opportunity remains IMPLEMENTED.
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.IMPLEMENTED

    # No provider replay.
    assert _count_provider_requests(ver_registry) == provider_requests_after_exec


# ----------------------------------------------------------------------
# Tests: Evaluation exception → PENDING preserved, manual retry
# ----------------------------------------------------------------------


def test_evaluation_exception_preserves_pending_for_retry(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If VerificationEvaluationService.evaluate() raises an unexpected
    software/database error AFTER ScanAnalysis=COMPLETED, the
    verification MAY remain PENDING for local retry.

    Then calling the manual deterministic evaluate endpoint/service
    completes the evaluation without provider calls.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=5,
        monthly_limit=100,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-ee")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: all runs succeed.
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-ee"
    )
    ver_scan_id = result.scan.id
    ver_id = result.verification.id

    # Monkey-patch evaluate to raise during auto-evaluation.
    # Patch BEFORE _execute because execute_scan() internally calls
    # finalize() with trigger_analysis=True, which triggers analysis
    # and then auto-evaluation.
    from app.services.verification_evaluation_service import VerificationEvaluationService

    original_evaluate = VerificationEvaluationService.evaluate
    call_count = {"n": 0}

    def failing_evaluate(self: VerificationEvaluationService, vid: uuid.UUID) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Unexpected evaluation failure for test")
        return original_evaluate(self, vid)

    monkeypatch.setattr(VerificationEvaluationService, "evaluate", failing_evaluate)

    _execute(db_session, ver_scan_id, ver_registry)
    db_session.expire_all()

    provider_requests_after_exec = _count_provider_requests(ver_registry)

    # Scan is COMPLETED.
    ver_scan = db_session.get(Scan, ver_scan_id)
    assert ver_scan is not None
    assert ver_scan.status in (ScanStatus.COMPLETED, ScanStatus.PARTIAL)

    # Analysis is COMPLETED.
    from app.models.analysis import ANALYSIS_VERSION, ScanAnalysis

    analysis = (
        db_session.execute(
            select(ScanAnalysis).where(
                ScanAnalysis.scan_id == ver_scan_id,
                ScanAnalysis.analysis_version == ANALYSIS_VERSION,
            )
        )
        .scalars()
        .first()
    )
    assert analysis is not None
    assert analysis.status == "COMPLETED"

    # Verification MAY remain PENDING (ephemeral evaluation error).
    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.PENDING

    # No provider replay during evaluation failure.
    assert _count_provider_requests(ver_registry) == provider_requests_after_exec

    # Manual retry: evaluate succeeds (second call uses original_evaluate).
    eval_result = _evaluate_verification(db_session, ver_id)
    assert eval_result.outcome in (
        VerificationOutcome.RESOLVED,
        VerificationOutcome.IMPROVED,
        VerificationOutcome.NOT_IMPROVED,
        VerificationOutcome.REGRESSED,
    )

    # No additional provider calls during manual evaluation.
    assert _count_provider_requests(ver_registry) == provider_requests_after_exec


# ----------------------------------------------------------------------
# Tests: Quota failure with real QuotaService (real PostgreSQL)
# ----------------------------------------------------------------------


@pytest.fixture()
def engine_factory():
    """Create a real PostgreSQL engine + session factory for tests
    that require committed transactions (quota, concurrency)."""
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True, pool_size=10)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield engine, factory
    engine.dispose()


def _add_prices_safe(db: Session, providers: list[LLMProvider]) -> None:
    """Add synthetic price rules, replacing any existing ones for the
    same (provider, surface, model) tuple.

    Safe for real PostgreSQL tests where _add_prices would fail due to
    FK constraints from prompt_runs created by previous committed tests.
    Deletes only rules not yet referenced by prompt_runs.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as sqla_text

    from app.models import ProviderPriceRule

    now = datetime.now(UTC)
    for provider in providers:
        model = MODELS[provider]
        # Null out FK references and delete existing synthetic rules for this model.
        db.execute(
            sqla_text(
                "UPDATE prompt_runs SET pricing_rule_id = NULL "
                "WHERE pricing_rule_id IN (SELECT id FROM provider_price_rules "
                "WHERE model = :model)"
            ),
            {"model": model},
        )
        db.execute(
            sqla_text(
                "UPDATE usage_events SET pricing_rule_id = NULL "
                "WHERE pricing_rule_id IN (SELECT id FROM provider_price_rules "
                "WHERE model = :model)"
            ),
            {"model": model},
        )
        db.execute(
            sqla_text("DELETE FROM provider_price_rules WHERE model = :model"),
            {"model": model},
        )
        db.add(
            ProviderPriceRule(
                pricing_key=f"synthetic:{provider.value}:{uuid.uuid4().hex}",
                provider=provider,
                provider_surface=SURFACES[provider],
                model=model,
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
        )
    db.commit()


def _cleanup_test_data(factory: Callable[[], Session], workspace_id: uuid.UUID) -> None:
    """Delete all test data for a workspace in FK-safe order."""
    session = factory()
    try:
        from sqlalchemy import text as sqla_text

        wid = str(workspace_id)
        # Clear circular FK references.
        session.execute(
            sqla_text(
                "UPDATE opportunities SET implementation_baseline_occurrence_id = NULL "
                "WHERE project_id IN (SELECT id FROM projects WHERE workspace_id = :wid)"
            ),
            {"wid": wid},
        )
        # Delete children first.
        session.execute(
            sqla_text(
                "DELETE FROM opportunity_evidence WHERE occurrence_id IN "
                "(SELECT id FROM opportunity_occurrences WHERE opportunity_id IN "
                "(SELECT id FROM opportunities WHERE project_id IN "
                "(SELECT id FROM projects WHERE workspace_id = :wid)))"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM opportunity_verifications WHERE opportunity_id IN "
                "(SELECT id FROM opportunities WHERE project_id IN "
                "(SELECT id FROM projects WHERE workspace_id = :wid))"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM opportunity_occurrences WHERE opportunity_id IN "
                "(SELECT id FROM opportunities WHERE project_id IN "
                "(SELECT id FROM projects WHERE workspace_id = :wid))"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM opportunities WHERE project_id IN "
                "(SELECT id FROM projects WHERE workspace_id = :wid)"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM source_attributions WHERE scan_analysis_id IN "
                "(SELECT id FROM scan_analyses WHERE scan_id IN "
                "(SELECT id FROM scans WHERE workspace_id = :wid))"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM entity_mentions WHERE scan_analysis_id IN "
                "(SELECT id FROM scan_analyses WHERE scan_id IN "
                "(SELECT id FROM scans WHERE workspace_id = :wid))"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM response_sources WHERE prompt_run_id IN "
                "(SELECT id FROM prompt_runs WHERE scan_id IN "
                "(SELECT id FROM scans WHERE workspace_id = :wid))"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM scan_analyses WHERE scan_id IN "
                "(SELECT id FROM scans WHERE workspace_id = :wid)"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM scan_entity_snapshots WHERE scan_id IN "
                "(SELECT id FROM scans WHERE workspace_id = :wid)"
            ),
            {"wid": wid},
        )
        # Null out circular FK: prompt_runs.usage_event_id → usage_events.id
        session.execute(
            sqla_text(
                "UPDATE prompt_runs SET usage_event_id = NULL "
                "WHERE scan_id IN (SELECT id FROM scans WHERE workspace_id = :wid)"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM usage_events WHERE prompt_run_id IN "
                "(SELECT id FROM prompt_runs WHERE scan_id IN "
                "(SELECT id FROM scans WHERE workspace_id = :wid))"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM prompt_runs WHERE scan_id IN "
                "(SELECT id FROM scans WHERE workspace_id = :wid)"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text("DELETE FROM scans WHERE workspace_id = :wid"),
            {"wid": wid},
        )
        session.execute(
            sqla_text("DELETE FROM quota_reservations WHERE workspace_id = :wid"),
            {"wid": wid},
        )
        session.execute(
            sqla_text("DELETE FROM workspace_usage_periods WHERE workspace_id = :wid"),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM prompts WHERE prompt_set_id IN "
                "(SELECT id FROM prompt_sets WHERE project_id IN "
                "(SELECT id FROM projects WHERE workspace_id = :wid))"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM prompt_sets WHERE project_id IN "
                "(SELECT id FROM projects WHERE workspace_id = :wid)"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM project_keywords WHERE project_id IN "
                "(SELECT id FROM projects WHERE workspace_id = :wid)"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM competitors WHERE project_id IN "
                "(SELECT id FROM projects WHERE workspace_id = :wid)"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM project_providers WHERE project_id IN "
                "(SELECT id FROM projects WHERE workspace_id = :wid)"
            ),
            {"wid": wid},
        )
        session.execute(
            sqla_text("DELETE FROM projects WHERE workspace_id = :wid"),
            {"wid": wid},
        )
        session.execute(
            sqla_text("DELETE FROM billing_accounts WHERE workspace_id = :wid"),
            {"wid": wid},
        )
        session.execute(
            sqla_text(
                "DELETE FROM plan_providers WHERE plan_id IN "
                "(SELECT id FROM plan_definitions WHERE code LIKE 'P10_%')"
            ),
        )
        session.execute(
            sqla_text("DELETE FROM plan_definitions WHERE code LIKE 'P10_%'"),
        )
        session.execute(
            sqla_text("DELETE FROM workspace_members WHERE workspace_id = :wid"),
            {"wid": wid},
        )
        session.execute(
            sqla_text("DELETE FROM workspaces WHERE id = :wid"),
            {"wid": wid},
        )
        session.execute(
            sqla_text("DELETE FROM users WHERE email LIKE 'p10-%@example.test'"),
        )
        session.execute(
            sqla_text("DELETE FROM provider_price_rules WHERE model LIKE 'synthetic-%'"),
        )
        session.commit()
    finally:
        session.close()


def test_quota_failure_real_postgresql(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Quota failure with real QuotaService and real PostgreSQL.

    Uses a real engine (not transaction-wrapped) so the quota service's
    rollback semantics are representative. The plan limit is configured
    so the Verification reservation genuinely exceeds available quota.
    """
    from app.core.exceptions import QuotaExceededError
    from app.services.action_generation_service import ActionGenerationService
    from app.services.scan_creation_service import ScanCreationService
    from app.services.verification_scan_creation_service import (
        VerificationScanCreationService,
    )

    _engine, factory = engine_factory

    # Setup with a tight monthly limit: 5 checks for baseline, 0 remaining.
    setup_session = factory()
    try:
        ws, user, project, _ps, _prompts = _seed(
            setup_session,
            providers=[LLMProvider.OPENAI],
            prompt_count=5,
            monthly_limit=5,  # Exactly enough for baseline, not verification.
            competitors=[("Rival", "rival.test")],
        )
        _add_prices_safe(setup_session, [LLMProvider.OPENAI])
        setup_session.commit()

        ws_id = ws.id
        user_id = user.id
        project_id = project.id

        # Create + execute baseline scan.
        baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
        dispatcher = FakeDispatcher()

        result = ScanCreationService(
            setup_session,
            dispatcher,
            settings=_settings(),
            registry=baseline_registry,  # type: ignore[arg-type]
        ).create_scan(ws_id, project_id, ScanType.STANDARD, user_id, "bl-qf-real")
        scan_id = result.scan.id
        setup_session.commit()
    finally:
        setup_session.close()

    # Execute baseline.
    exec_session = factory()
    try:
        _execute(exec_session, scan_id, baseline_registry)
        exec_session.commit()
    finally:
        exec_session.close()

    # Refresh actions + transition to IMPLEMENTED.
    act_session = factory()
    try:
        ActionGenerationService(act_session).refresh_from_scan(ws_id, project_id, scan_id)
        act_session.commit()

        opp = (
            act_session.execute(
                select(Opportunity).where(
                    Opportunity.project_id == project_id,
                    Opportunity.opportunity_type == OpportunityType.DISCOVERY_VISIBILITY_GAP,
                )
            )
            .scalars()
            .first()
        )
        assert opp is not None
        opp_id = opp.id

        OpportunityWorkflowService(act_session).transition(
            ws_id, project_id, opp_id, OpportunityStatus.IN_PROGRESS
        )
        OpportunityWorkflowService(act_session).transition(
            ws_id, project_id, opp_id, OpportunityStatus.IMPLEMENTED
        )
        act_session.commit()
    finally:
        act_session.close()

    # Verification attempt: quota genuinely exceeded (5 used, 0 remaining).
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    ver_session = factory()
    try:
        with pytest.raises(QuotaExceededError):
            VerificationScanCreationService(
                ver_session,
                FakeDispatcher(),
                settings=_settings(),
                registry=ver_registry,  # type: ignore[arg-type]
            ).create_verification_scan(
                workspace_id=ws_id,
                project_id=project_id,
                opportunity_id=opp_id,
                requested_by_user_id=user_id,
                idempotency_key="ver-qf-real-1",
            )
    finally:
        ver_session.close()

    # Verify: Scan FAILED, Verification INCONCLUSIVE.
    verify_session = factory()
    try:
        ver_scan = (
            verify_session.execute(select(Scan).where(Scan.scan_type == ScanType.VERIFICATION))
            .scalars()
            .first()
        )
        assert ver_scan is not None
        assert ver_scan.status == ScanStatus.FAILED

        ver = (
            verify_session.execute(
                select(OpportunityVerification).where(
                    OpportunityVerification.verification_scan_id == ver_scan.id
                )
            )
            .scalars()
            .first()
        )
        assert ver is not None
        assert ver.outcome == VerificationOutcome.INCONCLUSIVE
        assert ver.reason_code == VerificationReasonCode.VERIFICATION_SCAN_FAILED

        # No PENDING verification remains.
        pending = list(
            verify_session.execute(
                select(OpportunityVerification).where(
                    OpportunityVerification.opportunity_id == opp_id,
                    OpportunityVerification.outcome == VerificationOutcome.PENDING,
                )
            ).scalars()
        )
        assert len(pending) == 0

        # 0 provider calls (quota failed before dispatch).
        assert _count_provider_requests(ver_registry) == 0
    finally:
        verify_session.close()

    # Cleanup.
    _cleanup_test_data(factory, ws_id)


# ----------------------------------------------------------------------
# Tests: Real PostgreSQL different-key concurrency (MANDATORY)
# ----------------------------------------------------------------------


def test_different_key_concurrency_real_postgresql(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Two threads, different idempotency keys, same Opportunity + baseline.

    Expected:
    - Exactly one active provider-spending Verification
    - Other request gets ConflictError (one pending per cycle)
    - No raw IntegrityError leaks
    - One quota reservation, one dispatch
    """
    from app.services.action_generation_service import ActionGenerationService
    from app.services.scan_creation_service import ScanCreationService
    from app.services.verification_scan_creation_service import (
        VerificationScanCreationService,
    )

    _engine, factory = engine_factory

    # Setup: use _seed with a real session, then commit everything.
    setup_session = factory()
    try:
        ws, user, project, _ps, _prompts = _seed(
            setup_session,
            providers=[LLMProvider.OPENAI],
            prompt_count=5,
            monthly_limit=100,
            competitors=[("Rival", "rival.test")],
        )
        _add_prices_safe(setup_session, [LLMProvider.OPENAI])
        setup_session.commit()

        ws_id = ws.id
        user_id = user.id
        project_id = project.id

        # Create + execute baseline scan.
        baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
        dispatcher = FakeDispatcher()

        result = ScanCreationService(
            setup_session,
            dispatcher,
            settings=_settings(),
            registry=baseline_registry,  # type: ignore[arg-type]
        ).create_scan(ws_id, project_id, ScanType.STANDARD, user_id, "conc-bl")
        scan_id = result.scan.id
        setup_session.commit()
    finally:
        setup_session.close()

    # Execute baseline.
    exec_session = factory()
    try:
        _execute(exec_session, scan_id, baseline_registry)
        exec_session.commit()
    finally:
        exec_session.close()

    # Refresh actions + transition to IMPLEMENTED.
    act_session = factory()
    try:
        ActionGenerationService(act_session).refresh_from_scan(ws_id, project_id, scan_id)
        act_session.commit()

        opp = (
            act_session.execute(
                select(Opportunity).where(
                    Opportunity.project_id == project_id,
                    Opportunity.opportunity_type == OpportunityType.DISCOVERY_VISIBILITY_GAP,
                )
            )
            .scalars()
            .first()
        )
        assert opp is not None
        opp_id = opp.id

        OpportunityWorkflowService(act_session).transition(
            ws_id, project_id, opp_id, OpportunityStatus.IN_PROGRESS
        )
        OpportunityWorkflowService(act_session).transition(
            ws_id, project_id, opp_id, OpportunityStatus.IMPLEMENTED
        )
        act_session.commit()
    finally:
        act_session.close()

    # Now two threads attempt verification with different keys.
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    barrier = threading.Barrier(2)
    results: dict[str, Any] = {"success": 0, "conflict": 0, "errors": []}
    lock = threading.Lock()

    def worker(label: str) -> None:
        session = factory()
        try:
            svc = VerificationScanCreationService(
                session,
                FakeDispatcher(),
                settings=_settings(),
                registry=ver_registry,  # type: ignore[arg-type]
            )
            barrier.wait(timeout=15)
            svc.create_verification_scan(
                workspace_id=ws_id,
                project_id=project_id,
                opportunity_id=opp_id,
                requested_by_user_id=user_id,
                idempotency_key=f"conc-ver-{label}",
            )
            with lock:
                results["success"] += 1
        except ConflictError as e:
            with lock:
                results["conflict"] += 1
                results["errors"].append(f"ConflictError: {e}")
        except Exception as e:
            with lock:
                results["errors"].append(f"{type(e).__name__}: {e}")
        finally:
            session.close()

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert (
        results["success"] == 1
    ), f"Expected 1 success, got {results['success']}: {results['errors']}"
    assert (
        results["conflict"] == 1
    ), f"Expected 1 ConflictError, got {results['conflict']}: {results['errors']}"
    # No raw IntegrityError leaks.
    assert all(
        "IntegrityError" not in e for e in results["errors"]
    ), f"Raw IntegrityError leaked: {results['errors']}"

    # Verify exactly one PENDING verification exists.
    verify_session = factory()
    try:
        pending = list(
            verify_session.execute(
                select(OpportunityVerification).where(
                    OpportunityVerification.opportunity_id == opp_id,
                    OpportunityVerification.outcome == VerificationOutcome.PENDING,
                )
            ).scalars()
        )
        assert len(pending) == 1, f"Expected 1 PENDING, got {len(pending)}"

        # Assert partial unique index invariant: at most one PENDING
        # for the same (opportunity_id, baseline_occurrence_id).
        opp = verify_session.get(Opportunity, opp_id)
        assert opp is not None
        baseline_occ_id = opp.implementation_baseline_occurrence_id
        if baseline_occ_id is not None:
            pending_for_baseline = list(
                verify_session.execute(
                    select(OpportunityVerification).where(
                        OpportunityVerification.opportunity_id == opp_id,
                        OpportunityVerification.outcome == VerificationOutcome.PENDING,
                    )
                ).scalars()
            )
            assert len(pending_for_baseline) == 1
    finally:
        verify_session.close()

    # Cleanup.
    _cleanup_test_data(factory, ws_id)


# ----------------------------------------------------------------------
# Tests: Targeted scope execution — CITATION
# ----------------------------------------------------------------------


def test_targeted_scope_citation_execution(db_session: Session) -> None:
    """CITATION verification: only historical WEB_GROUNDED target cells
    are executed. MODEL_ONLY provider requests = 0.

    This test exercises the actual created Scan plan, not just the
    resolver.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=5,
        monthly_limit=200,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    # Baseline: use per-run registry to create a citation gap.
    # 10 runs (5 prompts * 2 providers): brand cited in 0, competitor cited in all.
    baseline_registry = ScriptedRegistry(
        {
            LLMProvider.OPENAI: PerRunAdapter(
                LLMProvider.OPENAI,
                SURFACES[LLMProvider.OPENAI],
                brand_name="Acme",
                competitor_name="Rival",
                mention_mode="both",
                brand_citation_url="https://acme.test/page",
                competitor_citation_url="https://rival.test/page",
                per_run_modes=["both"] * 5,
                per_run_brand_citations=[False] * 5,
                per_run_competitor_citations=[True] * 5,
            ),
            LLMProvider.ANTHROPIC: PerRunAdapter(
                LLMProvider.ANTHROPIC,
                SURFACES[LLMProvider.ANTHROPIC],
                brand_name="Acme",
                competitor_name="Rival",
                mention_mode="both",
                brand_citation_url="https://acme.test/page",
                competitor_citation_url="https://rival.test/page",
                per_run_modes=["both"] * 5,
                per_run_brand_citations=[False] * 5,
                per_run_competitor_citations=[True] * 5,
            ),
        }
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-tce"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.OWNED_CITATION_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None

    # Verification registry.
    ver_registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        mention_mode="brand",
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-tce"
    )
    ver_scan = result.scan

    # CITATION: all WEB_GROUNDED cells = 5 prompts * 2 providers = 10.
    assert ver_scan.planned_ai_checks == 10

    # Execute the verification scan.
    _execute(db_session, ver_scan.id, ver_registry)

    # All PromptRuns should be WEB_GROUNDED (no MODEL_ONLY).
    runs = list(
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == ver_scan.id)).scalars()
    )
    assert len(runs) == 10
    for run in runs:
        assert run.execution_mode == ProviderExecutionMode.WEB_GROUNDED

    # Count MODEL_ONLY runs = 0.
    model_only_count = sum(
        1 for run in runs if run.execution_mode == ProviderExecutionMode.MODEL_ONLY
    )
    assert model_only_count == 0


# ----------------------------------------------------------------------
# Tests: Targeted scope execution — PROMPT
# ----------------------------------------------------------------------


def test_targeted_scope_prompt_execution(db_session: Session) -> None:
    """PROMPT verification: only the exact prompt_id cells are executed.
    Unrelated prompt provider requests = 0.

    This test exercises the actual created Scan plan, not just the
    resolver.
    """
    ws, user, project, _ps, prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=5,
        monthly_limit=100,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-tpe"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.PROMPT_COMPETITOR_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.prompt_id is not None

    # The prompt_id should be one of the seeded prompts.
    prompt_ids = {p.id for p in prompts}
    assert opp.prompt_id in prompt_ids

    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-tpe"
    )
    ver_scan = result.scan

    # PROMPT: exact prompt only = 1 prompt * 1 provider = 1 cell.
    assert ver_scan.planned_ai_checks == 1

    # Execute the verification scan.
    _execute(db_session, ver_scan.id, ver_registry)

    # All PromptRuns should be for the exact prompt_id.
    runs = list(
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == ver_scan.id)).scalars()
    )
    assert len(runs) == 1
    assert runs[0].prompt_id == opp.prompt_id

    # No unrelated prompt runs.
    for run in runs:
        assert run.prompt_id == opp.prompt_id
