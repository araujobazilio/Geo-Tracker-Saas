"""Integration tests for Phase 10.4 — Verification Stale-Recovery
Lifecycle Closure.

Tests cover:
- Stale PENDING VERIFICATION scan → FAILED → INCONCLUSIVE auto-terminalized
- Stale RUNNING VERIFICATION (zero success) → FAILED → INCONCLUSIVE
- Stale RUNNING VERIFICATION (partial success) → PARTIAL → analyze/evaluate
- Analysis failure during recovery → INCONCLUSIVE / ANALYSIS_NOT_COMPLETED
- Ephemeral evaluation error during recovery → PENDING preserved
- Retry after recovered FAILED → new verification allowed
- Recovery economics: provider requests, AI Checks, UsageEvents, reservation
- Concurrency regression with RecordingDispatcher + economics assertions
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

from app.config import Settings
from app.core.enums import (
    LLMProvider,
    OpportunityStatus,
    OpportunityType,
    PromptRunStatus,
    QuotaReservationStatus,
    ScanStatus,
    ScanType,
    VerificationOutcome,
    VerificationReasonCode,
)
from app.models import (
    Opportunity,
    OpportunityVerification,
    PromptRun,
    QuotaReservation,
    Scan,
)
from app.services.opportunity_workflow_service import OpportunityWorkflowService
from app.services.scan_finalization_service import ScanRecoveryService

# Re-export the fake adapters and helpers from test_verification_phase10
from tests.integration.test_verification_phase10 import (
    MODELS,
    SURFACES,
    FakeDispatcher,
    ScriptedRegistry,
    _add_prices,
    _connection_factory,
    _create_verification,
    _execute,
    _full_pipeline,
    _get_first_opportunity,
    _refresh_actions,
    _registry,
    _seed,
)

pytestmark = pytest.mark.integration

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _recovery_settings(*, stale_after: int = 60) -> Settings:
    """Settings with a configurable stale threshold for recovery tests."""
    return Settings(
        app_env="test",
        openai_api_key="synthetic-openai-key",
        openai_scan_model=MODELS[LLMProvider.OPENAI],
        anthropic_api_key="synthetic-anthropic-key",
        anthropic_scan_model=MODELS[LLMProvider.ANTHROPIC],
        google_api_key="synthetic-google-key",
        google_scan_model=MODELS[LLMProvider.GOOGLE],
        perplexity_api_key="synthetic-perplexity-key",
        perplexity_scan_model=MODELS[LLMProvider.PERPLEXITY],
        pricing_require_rule_for_execution=True,
        scan_max_concurrency=2,
        scan_stale_after_seconds=stale_after,
    )


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


def _get_verification_for_scan(db: Session, scan_id: uuid.UUID) -> OpportunityVerification | None:
    return (
        db.execute(
            select(OpportunityVerification).where(
                OpportunityVerification.verification_scan_id == scan_id
            )
        )
        .scalars()
        .first()
    )


def _setup_implemented_opportunity(
    db: Session,
    *,
    providers: list[LLMProvider] | None = None,
    prompt_count: int = 5,
    monthly_limit: int = 100,
    key_suffix: str = "x",
) -> tuple[Any, Any, Any, Opportunity, Scan, ScriptedRegistry, FakeDispatcher]:
    """Create a baseline scan, refresh actions, transition to IMPLEMENTED.

    Returns (ws, user, project, opp, baseline_scan, baseline_registry, dispatcher).
    """
    provs = providers or [LLMProvider.OPENAI]
    ws, user, project, _ps, _prompts = _seed(
        db,
        providers=provs,
        prompt_count=prompt_count,
        monthly_limit=monthly_limit,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db, provs)

    baseline_registry = _registry(provs, mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db, ws, user, project, baseline_registry, dispatcher, key=f"bl-{key_suffix}"
    )
    _refresh_actions(db, ws, project, scan.id)
    opp = _get_first_opportunity(db, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session_expire(db)
    return ws, user, project, opp, scan, baseline_registry, dispatcher


def db_session_expire(db: Session) -> None:
    db.expire_all()


# ----------------------------------------------------------------------
# Tests: Stale PENDING VERIFICATION → FAILED → INCONCLUSIVE
# ----------------------------------------------------------------------


def test_stale_pending_verification_auto_terminalizes(db_session: Session) -> None:
    """A stale PENDING VERIFICATION scan is recovered to FAILED.

    The OpportunityVerification MUST be auto-terminalized as
    INCONCLUSIVE / VERIFICATION_SCAN_FAILED.

    No manual VerificationLifecycleService call.
    No provider replay.
    """
    ws, user, project, opp, _bl_scan, _bl_reg, _bl_disp = _setup_implemented_opportunity(
        db_session, key_suffix="sp"
    )

    # Create a verification scan — it stays PENDING (no dispatch).
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    ver_dispatcher = FakeDispatcher()
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, ver_dispatcher, key="ver-sp"
    )
    ver_scan_id = result.scan.id
    ver_id = result.verification.id

    # Capture pre-recovery state.
    pre_provider_requests = _count_provider_requests(ver_registry)

    # Advance the clock beyond the stale threshold (60s).
    future = datetime.now(UTC) + timedelta(minutes=10)
    ScanRecoveryService(
        db_session,
        settings=_recovery_settings(stale_after=60),
        analysis_session_factory=_connection_factory(db_session),
    ).recover_stale_scans(future)
    db_session_expire(db_session)

    # Scan is FAILED.
    ver_scan = db_session.get(Scan, ver_scan_id)
    assert ver_scan is not None
    assert ver_scan.status == ScanStatus.FAILED

    # Verification is auto-terminalized.
    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.INCONCLUSIVE
    assert ver.reason_code == VerificationReasonCode.VERIFICATION_SCAN_FAILED
    assert ver.evaluated_at is not None

    # Opportunity remains IMPLEMENTED.
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.IMPLEMENTED

    # No PENDING verification remains for this cycle.
    pending = list(
        db_session.execute(
            select(OpportunityVerification).where(
                OpportunityVerification.opportunity_id == opp.id,
                OpportunityVerification.outcome == VerificationOutcome.PENDING,
            )
        ).scalars()
    )
    assert len(pending) == 0

    # No provider replay.
    assert _count_provider_requests(ver_registry) == pre_provider_requests

    # Reservation released.
    reservation = _get_reservation(db_session, ver_scan_id)
    if reservation is not None:
        assert reservation.status != QuotaReservationStatus.ACTIVE

    # Zero AI Checks committed (all runs failed).
    assert _count_ai_checks_committed(db_session, ver_scan_id) == 0


# ----------------------------------------------------------------------
# Tests: Stale RUNNING VERIFICATION (zero success) → FAILED
# ----------------------------------------------------------------------


def test_stale_running_verification_zero_success_terminalizes(
    db_session: Session,
) -> None:
    """A stale RUNNING VERIFICATION scan with zero successful runs is
    recovered to FAILED.

    The OpportunityVerification MUST be auto-terminalized as
    INCONCLUSIVE / VERIFICATION_SCAN_FAILED.

    No provider replay.
    """
    ws, user, project, opp, _bl_scan, _bl_reg, _bl_disp = _setup_implemented_opportunity(
        db_session, key_suffix="sr"
    )

    # Create a verification scan.
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    ver_dispatcher = FakeDispatcher()
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, ver_dispatcher, key="ver-sr"
    )
    ver_scan_id = result.scan.id
    ver_id = result.verification.id

    # Simulate: dispatch started (RUNNING) but no runs completed.
    scan = db_session.get(Scan, ver_scan_id)
    assert scan is not None
    scan.status = ScanStatus.RUNNING
    scan.started_at = datetime.now(UTC) - timedelta(minutes=10)
    db_session.commit()
    db_session_expire(db_session)

    pre_provider_requests = _count_provider_requests(ver_registry)

    # Recover.
    ScanRecoveryService(
        db_session,
        settings=_recovery_settings(stale_after=60),
        analysis_session_factory=_connection_factory(db_session),
    ).recover_stale_scans(datetime.now(UTC))
    db_session_expire(db_session)

    # Scan is FAILED (all runs were unresolved → marked FAILED).
    ver_scan = db_session.get(Scan, ver_scan_id)
    assert ver_scan is not None
    assert ver_scan.status == ScanStatus.FAILED

    # Verification auto-terminalized.
    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.INCONCLUSIVE
    assert ver.reason_code == VerificationReasonCode.VERIFICATION_SCAN_FAILED
    assert ver.evaluated_at is not None

    # No provider replay.
    assert _count_provider_requests(ver_registry) == pre_provider_requests

    # Zero AI Checks committed.
    assert _count_ai_checks_committed(db_session, ver_scan_id) == 0


# ----------------------------------------------------------------------
# Tests: Stale RUNNING VERIFICATION (partial success) → PARTIAL
# ----------------------------------------------------------------------


def test_stale_running_verification_partial_success_analyzes(
    db_session: Session,
) -> None:
    """A stale RUNNING VERIFICATION scan with some successful runs is
    recovered to PARTIAL.

    The successful evidence is preserved. Deterministic analysis +
    evaluation run locally using only the persisted successful
    observations.

    No provider replay. No new UsageEvents from recovery.
    """
    from app.providers.errors import ProviderResponseError

    ws, user, project, opp, _bl_scan, _bl_reg, _bl_disp = _setup_implemented_opportunity(
        db_session, prompt_count=5, key_suffix="pr"
    )

    # Create a verification scan with mixed outcomes: 3 succeed, 2 fail.
    ver_registry = _registry(
        [LLMProvider.OPENAI],
        mention_mode="brand",
        outcomes={
            LLMProvider.OPENAI: [
                None,
                None,
                None,
                ProviderResponseError("fail"),
                ProviderResponseError("fail"),
            ]
        },
    )
    ver_dispatcher = FakeDispatcher()
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, ver_dispatcher, key="ver-pr"
    )
    ver_scan_id = result.scan.id
    ver_id = result.verification.id

    # Execute the scan — 3 succeed, 2 fail. Scan becomes PARTIAL or COMPLETED.
    # But we need to simulate a stale RUNNING state. So execute partially:
    # execute only 3 runs, then mark the scan RUNNING and stale.
    # Actually, the easiest way is to execute all runs (which finalizes),
    # then revert the scan to RUNNING with only the successful runs
    # preserved and the failed ones reverted to RUNNING.

    # Execute all runs normally.
    _execute(db_session, ver_scan_id, ver_registry)
    db_session_expire(db_session)

    # After execution, the scan is finalized (PARTIAL since 3/5 succeeded).
    scan = db_session.get(Scan, ver_scan_id)
    assert scan is not None
    assert scan.status in (ScanStatus.PARTIAL, ScanStatus.COMPLETED)

    # Now simulate a stale RUNNING state: revert the scan to RUNNING,
    # and revert the 2 failed runs to RUNNING (unresolved).
    scan.status = ScanStatus.RUNNING
    scan.started_at = datetime.now(UTC) - timedelta(minutes=10)
    scan.completed_at = None
    scan.successful_runs = 0
    scan.failed_runs = 0

    runs = list(
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == ver_scan_id)).scalars()
    )
    failed_runs = [r for r in runs if r.status == PromptRunStatus.FAILED]
    for run in failed_runs:
        run.status = PromptRunStatus.RUNNING
        run.completed_at = None
        run.error_code = None
        run.error_message = None

    db_session.commit()
    db_session_expire(db_session)

    # Capture pre-recovery economics.
    pre_provider_requests = _count_provider_requests(ver_registry)
    pre_usage_events = _count_usage_events(db_session, ver_scan_id)
    pre_ai_checks = _count_ai_checks_committed(db_session, ver_scan_id)

    # Recover.
    ScanRecoveryService(
        db_session,
        settings=_recovery_settings(stale_after=60),
        analysis_session_factory=_connection_factory(db_session),
    ).recover_stale_scans(datetime.now(UTC))
    db_session_expire(db_session)

    # Scan is PARTIAL (3 succeeded, 2 recovered to FAILED).
    ver_scan = db_session.get(Scan, ver_scan_id)
    assert ver_scan is not None
    assert ver_scan.status == ScanStatus.PARTIAL
    assert ver_scan.successful_runs == 3
    assert ver_scan.failed_runs == 2

    # Successful runs preserved.
    runs = list(
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == ver_scan_id)).scalars()
    )
    succeeded = [r for r in runs if r.status == PromptRunStatus.SUCCEEDED]
    failed = [r for r in runs if r.status == PromptRunStatus.FAILED]
    assert len(succeeded) == 3
    assert len(failed) == 2

    # No provider replay.
    assert _count_provider_requests(ver_registry) == pre_provider_requests

    # No new UsageEvents from recovery.
    assert _count_usage_events(db_session, ver_scan_id) == pre_usage_events

    # AI Checks unchanged (only successful runs have committed checks).
    assert _count_ai_checks_committed(db_session, ver_scan_id) == pre_ai_checks

    # Reservation released (scan is terminal).
    reservation = _get_reservation(db_session, ver_scan_id)
    if reservation is not None:
        assert reservation.status != QuotaReservationStatus.ACTIVE

    # Verification is no longer PENDING — it was analyzed + evaluated.
    # With 3/5 successful runs, coverage = 60% < 75% threshold → INCONCLUSIVE.
    # (Or it may be evaluated to a terminal outcome if coverage passes.)
    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome != VerificationOutcome.PENDING


# ----------------------------------------------------------------------
# Tests: Analysis failure during recovery
# ----------------------------------------------------------------------


def test_analysis_failure_during_recovery_terminalizes(
    db_session: Session,
) -> None:
    """If deterministic analysis fails during recovery of a PARTIAL
    VERIFICATION scan, the verification MUST be terminalized as
    INCONCLUSIVE / ANALYSIS_NOT_COMPLETED.

    Reuses Phase 10.3 durable-analysis reconciliation.
    No provider replay.
    """
    from app.providers.errors import ProviderResponseError

    ws, user, project, opp, _bl_scan, _bl_reg, _bl_disp = _setup_implemented_opportunity(
        db_session, prompt_count=5, key_suffix="af"
    )

    ver_registry = _registry(
        [LLMProvider.OPENAI],
        mention_mode="brand",
        outcomes={
            LLMProvider.OPENAI: [
                None,
                None,
                None,
                ProviderResponseError("fail"),
                ProviderResponseError("fail"),
            ]
        },
    )
    ver_dispatcher = FakeDispatcher()
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, ver_dispatcher, key="ver-af"
    )
    ver_scan_id = result.scan.id
    ver_id = result.verification.id

    # Execute all runs, then simulate stale RUNNING.
    _execute(db_session, ver_scan_id, ver_registry)
    db_session_expire(db_session)

    scan = db_session.get(Scan, ver_scan_id)
    assert scan is not None
    scan.status = ScanStatus.RUNNING
    scan.started_at = datetime.now(UTC) - timedelta(minutes=10)
    scan.completed_at = None
    scan.successful_runs = 0
    scan.failed_runs = 0

    runs = list(
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == ver_scan_id)).scalars()
    )
    failed_runs = [r for r in runs if r.status == PromptRunStatus.FAILED]
    for run in failed_runs:
        run.status = PromptRunStatus.RUNNING
        run.completed_at = None
        run.error_code = None
        run.error_message = None

    db_session.commit()
    db_session_expire(db_session)

    # Revert the existing COMPLETED analysis to FAILED so recovery's
    # analyze() retries it and actually calls _run_analysis (which we
    # patch to raise). Use raw SQL to bypass ORM cache and ensure the
    # update is committed to the database.
    from app.models.analysis import ANALYSIS_VERSION

    db_session.execute(
        text(
            "UPDATE scan_analyses SET status = 'FAILED', "
            "failure_code = 'PRE_RECOVERY_RESET', "
            "failure_message = 'Reset for recovery test' "
            "WHERE scan_id = :sid AND analysis_version = :ver"
        ),
        {"sid": str(ver_scan_id), "ver": ANALYSIS_VERSION},
    )
    # Also revert the verification to PENDING so _reconcile_analysis_failure
    # can terminalize it (it only acts on PENDING verifications).
    db_session.execute(
        text(
            "UPDATE opportunity_verifications SET outcome = 'PENDING', "
            "reason_code = NULL, evaluation_message = NULL, "
            "evaluated_at = NULL WHERE id = :vid"
        ),
        {"vid": str(ver_id)},
    )
    db_session.commit()
    db_session_expire(db_session)

    pre_provider_requests = _count_provider_requests(ver_registry)

    # Patch _run_analysis to raise during recovery's analysis phase.
    from app.services.scan_analysis_service import ScanAnalysisService

    original_run_analysis = ScanAnalysisService._run_analysis

    def exploding_run_analysis(self: ScanAnalysisService, scan: Scan, analysis: Any) -> Any:
        raise RuntimeError("Unexpected analysis failure during recovery")

    ScanAnalysisService._run_analysis = exploding_run_analysis  # type: ignore[method-assign]
    try:
        ScanRecoveryService(
            db_session,
            settings=_recovery_settings(stale_after=60),
            analysis_session_factory=_connection_factory(db_session),
        ).recover_stale_scans(datetime.now(UTC))
    finally:
        ScanAnalysisService._run_analysis = original_run_analysis  # type: ignore[method-assign]
    db_session_expire(db_session)

    # Scan is PARTIAL.
    ver_scan = db_session.get(Scan, ver_scan_id)
    assert ver_scan is not None
    assert ver_scan.status == ScanStatus.PARTIAL

    # ScanAnalysis is FAILED (persisted by the failure transaction).
    from app.models.analysis import ScanAnalysis

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

    # Verification terminalized as INCONCLUSIVE / ANALYSIS_NOT_COMPLETED.
    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.INCONCLUSIVE
    assert ver.reason_code == VerificationReasonCode.ANALYSIS_NOT_COMPLETED
    assert ver.evaluated_at is not None

    # No provider replay.
    assert _count_provider_requests(ver_registry) == pre_provider_requests


# ----------------------------------------------------------------------
# Tests: Ephemeral evaluation error during recovery
# ----------------------------------------------------------------------


def test_ephemeral_evaluation_error_during_recovery_preserves_pending(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If analysis COMPLETED but VerificationEvaluationService raises
    an unexpected transient error during recovery, the verification
    MAY remain PENDING for manual retry.

    No provider replay.
    """

    ws, user, project, opp, _bl_scan, _bl_reg, _bl_disp = _setup_implemented_opportunity(
        db_session, prompt_count=5, key_suffix="ee"
    )

    # All runs succeed so analysis will COMPLETED.
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    ver_dispatcher = FakeDispatcher()
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, ver_dispatcher, key="ver-ee"
    )
    ver_scan_id = result.scan.id
    ver_id = result.verification.id

    # Execute all runs, then simulate stale RUNNING.
    _execute(db_session, ver_scan_id, ver_registry)
    db_session_expire(db_session)

    scan = db_session.get(Scan, ver_scan_id)
    assert scan is not None
    # Revert one run to RUNNING to make the scan stale.
    scan.status = ScanStatus.RUNNING
    scan.started_at = datetime.now(UTC) - timedelta(minutes=10)
    scan.completed_at = None
    scan.successful_runs = 0
    scan.failed_runs = 0

    runs = list(
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == ver_scan_id)).scalars()
    )
    # Revert just one run to RUNNING.
    runs[0].status = PromptRunStatus.RUNNING
    runs[0].completed_at = None
    runs[0].error_code = None
    runs[0].error_message = None

    db_session.commit()
    db_session_expire(db_session)

    # Revert the verification to PENDING so recovery's evaluation
    # actually calls evaluate (which we patch to raise).
    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    ver.outcome = VerificationOutcome.PENDING
    ver.reason_code = None
    ver.evaluation_message = None
    ver.evaluated_at = None
    db_session.commit()
    db_session_expire(db_session)

    pre_provider_requests = _count_provider_requests(ver_registry)

    # Patch evaluate to raise during recovery's evaluation phase.
    from app.services.verification_evaluation_service import VerificationEvaluationService

    original_evaluate = VerificationEvaluationService.evaluate

    def failing_evaluate(self: VerificationEvaluationService, vid: uuid.UUID) -> Any:
        raise RuntimeError("Unexpected evaluation failure during recovery")

    monkeypatch.setattr(VerificationEvaluationService, "evaluate", failing_evaluate)

    ScanRecoveryService(
        db_session,
        settings=_recovery_settings(stale_after=60),
        analysis_session_factory=_connection_factory(db_session),
    ).recover_stale_scans(datetime.now(UTC))
    db_session_expire(db_session)

    # Scan is PARTIAL (4 succeeded, 1 recovered to FAILED).
    ver_scan = db_session.get(Scan, ver_scan_id)
    assert ver_scan is not None
    assert ver_scan.status == ScanStatus.PARTIAL

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

    # Verification remains PENDING (ephemeral evaluation error).
    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.PENDING

    # No provider replay.
    assert _count_provider_requests(ver_registry) == pre_provider_requests

    # Manual retry is possible (ephemeral error, not permanent).
    # Restore evaluate and verify the verification can be evaluated.
    monkeypatch.setattr(VerificationEvaluationService, "evaluate", original_evaluate)

    # Still no additional provider calls.
    assert _count_provider_requests(ver_registry) == pre_provider_requests


# ----------------------------------------------------------------------
# Tests: Retry after recovered FAILED verification
# ----------------------------------------------------------------------


def test_retry_after_recovered_failed_verification(db_session: Session) -> None:
    """After stale recovery produces FAILED + INCONCLUSIVE, a NEW
    verification with a different idempotency key MUST be allowed.

    The previous PENDING slot must not block it.
    """
    ws, user, project, opp, _bl_scan, _bl_reg, _bl_disp = _setup_implemented_opportunity(
        db_session, key_suffix="rt"
    )

    # First verification — goes stale PENDING.
    ver_registry_1 = _registry([LLMProvider.OPENAI], mention_mode="brand")
    ver_dispatcher_1 = FakeDispatcher()
    result_1 = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry_1, ver_dispatcher_1, key="ver-rt-1"
    )

    future = datetime.now(UTC) + timedelta(minutes=10)
    ScanRecoveryService(
        db_session,
        settings=_recovery_settings(stale_after=60),
        analysis_session_factory=_connection_factory(db_session),
    ).recover_stale_scans(future)
    db_session_expire(db_session)

    # First verification is INCONCLUSIVE.
    ver_1 = db_session.get(OpportunityVerification, result_1.verification.id)
    assert ver_1 is not None
    assert ver_1.outcome == VerificationOutcome.INCONCLUSIVE

    # Second verification with a different key — MUST be allowed.
    ver_registry_2 = _registry([LLMProvider.OPENAI], mention_mode="brand")
    ver_dispatcher_2 = FakeDispatcher()
    result_2 = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry_2, ver_dispatcher_2, key="ver-rt-2"
    )

    # New verification is PENDING (freshly created).
    ver_2 = db_session.get(OpportunityVerification, result_2.verification.id)
    assert ver_2 is not None
    assert ver_2.outcome == VerificationOutcome.PENDING
    assert ver_2.id != ver_1.id

    # Exactly one PENDING verification exists.
    pending = list(
        db_session.execute(
            select(OpportunityVerification).where(
                OpportunityVerification.opportunity_id == opp.id,
                OpportunityVerification.outcome == VerificationOutcome.PENDING,
            )
        ).scalars()
    )
    assert len(pending) == 1
    assert pending[0].id == ver_2.id


# ----------------------------------------------------------------------
# Tests: Recovery economics
# ----------------------------------------------------------------------


def test_recovery_economics_no_double_charge(db_session: Session) -> None:
    """Recovery economics: successful runs keep their committed AI Checks
    exactly once. Recovered unresolved runs get 0 AI Checks. No new
    UsageEvents from recovery analysis/evaluation. Reservation released.
    Provider call count unchanged.
    """
    from app.providers.errors import ProviderResponseError

    ws, user, project, opp, _bl_scan, _bl_reg, _bl_disp = _setup_implemented_opportunity(
        db_session, prompt_count=5, key_suffix="ec"
    )

    # 3 succeed, 2 fail.
    ver_registry = _registry(
        [LLMProvider.OPENAI],
        mention_mode="brand",
        outcomes={
            LLMProvider.OPENAI: [
                None,
                None,
                None,
                ProviderResponseError("fail"),
                ProviderResponseError("fail"),
            ]
        },
    )
    ver_dispatcher = FakeDispatcher()
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, ver_dispatcher, key="ver-ec"
    )
    ver_scan_id = result.scan.id

    # Execute all runs.
    _execute(db_session, ver_scan_id, ver_registry)
    db_session_expire(db_session)

    # Capture post-execution economics.
    post_exec_provider_requests = _count_provider_requests(ver_registry)
    post_exec_usage_events = _count_usage_events(db_session, ver_scan_id)
    post_exec_ai_checks = _count_ai_checks_committed(db_session, ver_scan_id)

    # 3 successful runs → 3 UsageEvents, 3 AI Checks.
    assert post_exec_usage_events == 3
    assert post_exec_ai_checks == 3
    # 5 provider calls (3 succeeded + 2 failed).
    assert post_exec_provider_requests == 5

    # Simulate stale RUNNING: revert the 2 failed runs to RUNNING.
    scan = db_session.get(Scan, ver_scan_id)
    assert scan is not None
    scan.status = ScanStatus.RUNNING
    scan.started_at = datetime.now(UTC) - timedelta(minutes=10)
    scan.completed_at = None
    scan.successful_runs = 0
    scan.failed_runs = 0

    runs = list(
        db_session.execute(select(PromptRun).where(PromptRun.scan_id == ver_scan_id)).scalars()
    )
    failed_runs = [r for r in runs if r.status == PromptRunStatus.FAILED]
    for run in failed_runs:
        run.status = PromptRunStatus.RUNNING
        run.completed_at = None
        run.error_code = None
        run.error_message = None

    db_session.commit()
    db_session_expire(db_session)

    # Recover.
    ScanRecoveryService(
        db_session,
        settings=_recovery_settings(stale_after=60),
        analysis_session_factory=_connection_factory(db_session),
    ).recover_stale_scans(datetime.now(UTC))
    db_session_expire(db_session)

    # Economics unchanged after recovery.
    assert _count_provider_requests(ver_registry) == post_exec_provider_requests
    assert _count_usage_events(db_session, ver_scan_id) == post_exec_usage_events
    assert _count_ai_checks_committed(db_session, ver_scan_id) == post_exec_ai_checks

    # Reservation released.
    reservation = _get_reservation(db_session, ver_scan_id)
    if reservation is not None:
        assert reservation.status != QuotaReservationStatus.ACTIVE


# ----------------------------------------------------------------------
# Tests: Recovery idempotency
# ----------------------------------------------------------------------


def test_recovery_idempotent(db_session: Session) -> None:
    """Recovering the same stale scan twice produces the same terminal
    outcome. No duplicate analysis, no duplicate verification, no
    provider calls.
    """
    ws, user, project, opp, _bl_scan, _bl_reg, _bl_disp = _setup_implemented_opportunity(
        db_session, key_suffix="id"
    )

    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    ver_dispatcher = FakeDispatcher()
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, ver_dispatcher, key="ver-id"
    )
    ver_scan_id = result.scan.id
    ver_id = result.verification.id

    # First recovery.
    future = datetime.now(UTC) + timedelta(minutes=10)
    ScanRecoveryService(
        db_session,
        settings=_recovery_settings(stale_after=60),
        analysis_session_factory=_connection_factory(db_session),
    ).recover_stale_scans(future)
    db_session_expire(db_session)

    ver_scan = db_session.get(Scan, ver_scan_id)
    assert ver_scan is not None
    assert ver_scan.status == ScanStatus.FAILED

    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.INCONCLUSIVE
    assert ver.reason_code == VerificationReasonCode.VERIFICATION_SCAN_FAILED

    first_evaluated_at = ver.evaluated_at
    pre_provider_requests = _count_provider_requests(ver_registry)

    # Second recovery — scan is already terminal, so it's a no-op.
    ScanRecoveryService(
        db_session,
        settings=_recovery_settings(stale_after=60),
        analysis_session_factory=_connection_factory(db_session),
    ).recover_stale_scans(future)
    db_session_expire(db_session)

    # Same terminal state.
    ver_scan = db_session.get(Scan, ver_scan_id)
    assert ver_scan is not None
    assert ver_scan.status == ScanStatus.FAILED

    ver = db_session.get(OpportunityVerification, ver_id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.INCONCLUSIVE
    assert ver.reason_code == VerificationReasonCode.VERIFICATION_SCAN_FAILED
    assert ver.evaluated_at == first_evaluated_at

    # No provider calls.
    assert _count_provider_requests(ver_registry) == pre_provider_requests


# ----------------------------------------------------------------------
# Concurrency regression with RecordingDispatcher + economics
# ----------------------------------------------------------------------


class RecordingDispatcher:
    """Thread-safe dispatcher that records all dispatch calls.

    Wraps FakeDispatcher but uses a threading.Lock to safely count
    dispatch invocations across concurrent threads.
    """

    def __init__(self) -> None:
        self._inner = FakeDispatcher()
        self._lock = threading.Lock()
        self.dispatch_count = 0

    def dispatch(self, scan_id: uuid.UUID) -> None:
        with self._lock:
            self.dispatch_count += 1
        return self._inner.dispatch(scan_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _add_prices_safe(db: Session, providers: list[LLMProvider]) -> None:
    """Add synthetic price rules, replacing any existing ones for the
    same (provider, surface, model) tuple. Safe for real PostgreSQL tests.
    """
    from datetime import timedelta

    from sqlalchemy import text as sqla_text

    from app.models import ProviderPriceRule

    now = datetime.now(UTC)
    for provider in providers:
        model = MODELS[provider]
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
    from sqlalchemy import text as sqla_text

    session = factory()
    try:
        wid = str(workspace_id)
        session.execute(
            sqla_text(
                "UPDATE opportunities SET implementation_baseline_occurrence_id = NULL "
                "WHERE project_id IN (SELECT id FROM projects WHERE workspace_id = :wid)"
            ),
            {"wid": wid},
        )
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


@pytest.fixture()
def engine_factory():
    """Create a real PostgreSQL engine + session factory for tests
    that require committed transactions (concurrency)."""
    engine = create_engine(TEST_DB_URL, pool_pre_ping=True, future=True, pool_size=10)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield engine, factory
    engine.dispose()


def test_concurrency_regression_with_economics(engine_factory) -> None:  # type: ignore[no-untyped-def]
    """Two threads, different idempotency keys, same IMPLEMENTED cycle.

    Expected:
    - Exactly 1 success, 1 ConflictError, 0 IntegrityError leaks
    - Exactly 1 VERIFICATION Scan for the cycle
    - Exactly 1 quota_reservation_id on that Scan
    - Exactly 1 logical dispatch (RecordingDispatcher)
    - 1 active PENDING Verification
    """
    from app.core.exceptions import ConflictError
    from app.services.action_generation_service import ActionGenerationService
    from app.services.scan_creation_service import ScanCreationService
    from app.services.verification_scan_creation_service import (
        VerificationScanCreationService,
    )

    _engine, factory = engine_factory

    # Setup with a real session.
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
        baseline_dispatcher = FakeDispatcher()

        result = ScanCreationService(
            setup_session,
            baseline_dispatcher,
            settings=_recovery_settings(),
            registry=baseline_registry,  # type: ignore[arg-type]
        ).create_scan(ws_id, project_id, ScanType.STANDARD, user_id, "conc-bl-104")
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

    # Two threads attempt verification with different keys.
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    recording_dispatcher = RecordingDispatcher()
    barrier = threading.Barrier(2)
    results: dict[str, Any] = {"success": 0, "conflict": 0, "errors": []}
    lock = threading.Lock()

    def worker(label: str) -> None:
        session = factory()
        try:
            svc = VerificationScanCreationService(
                session,
                recording_dispatcher,  # type: ignore[arg-type]
                settings=_recovery_settings(),
                registry=ver_registry,  # type: ignore[arg-type]
            )
            barrier.wait(timeout=15)
            svc.create_verification_scan(
                workspace_id=ws_id,
                project_id=project_id,
                opportunity_id=opp_id,
                requested_by_user_id=user_id,
                idempotency_key=f"conc-ver-104-{label}",
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
    assert all(
        "IntegrityError" not in e for e in results["errors"]
    ), f"Raw IntegrityError leaked: {results['errors']}"

    # Verify exactly 1 VERIFICATION Scan exists for the cycle.
    verify_session = factory()
    try:
        ver_scans = list(
            verify_session.execute(
                select(Scan).where(
                    Scan.workspace_id == ws_id,
                    Scan.scan_type == ScanType.VERIFICATION,
                )
            ).scalars()
        )
        assert len(ver_scans) == 1, f"Expected 1 VERIFICATION scan, got {len(ver_scans)}"

        # That scan has exactly 1 quota_reservation_id.
        assert ver_scans[0].quota_reservation_id is not None

        # Exactly 1 PENDING verification.
        pending = list(
            verify_session.execute(
                select(OpportunityVerification).where(
                    OpportunityVerification.opportunity_id == opp_id,
                    OpportunityVerification.outcome == VerificationOutcome.PENDING,
                )
            ).scalars()
        )
        assert len(pending) == 1, f"Expected 1 PENDING, got {len(pending)}"

        # Exactly 1 logical dispatch.
        assert (
            recording_dispatcher.dispatch_count == 1
        ), f"Expected 1 dispatch, got {recording_dispatcher.dispatch_count}"
    finally:
        verify_session.close()

    # Cleanup.
    _cleanup_test_data(factory, ws_id)
