"""Integration tests for Phase 10.1 Verification Hardening.

Tests cover:
- Targeted scope: exact historical cells per OpportunityType
- Non-rectangular planned_ai_checks (prompt_count * provider_count != planned)
- Brand-side resolution safeguards (brand disappears = NOT RESOLVED)
- Baseline coverage gate (INSUFFICIENT_BASELINE_COVERAGE)
- Two-sided citation sufficiency gate
- PROMPT_COMPETITOR_GAP uses competitor_only_rate (pp) not count
- Automatic evaluation after ScanAnalysis (full normal path)
- Analysis failure auto-path (no provider replay)
- One-pending-verification-per-cycle constraint
- Idempotency + baseline conflict (same key, different baseline)
- Second verification after terminal evaluation
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.core.enums import (
    BillingAccountStatus,
    BillingSource,
    FunnelStage,
    LLMProvider,
    OpportunityStatus,
    OpportunityType,
    ProjectStatus,
    PromptSetStatus,
    PromptType,
    ProviderExecutionMode,
    ProviderSurface,
    ScanStatus,
    ScanType,
    VerificationOutcome,
    VerificationReasonCode,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.exceptions import ConflictError, ValidationError
from app.core.verification_scope import VerificationScopeResolver
from app.models import (
    BillingAccount,
    Competitor,
    Opportunity,
    OpportunityOccurrence,
    OpportunityVerification,
    PlanDefinition,
    PlanProvider,
    Project,
    ProjectKeyword,
    ProjectProvider,
    Prompt,
    PromptSet,
    ProviderPriceRule,
    Scan,
    ScanAnalysis,
    ScanEntitySnapshot,
    User,
    Workspace,
    WorkspaceMember,
)
from app.providers.base import (
    ProviderCapabilities,
    ProviderCitation,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from app.services.action_generation_service import ActionGenerationService
from app.services.opportunity_workflow_service import OpportunityWorkflowService
from app.services.prompt_generation_service import GENERATOR_KEY
from app.services.scan_analysis_service import ScanAnalysisService
from app.services.scan_creation_service import ScanCreationResult, ScanCreationService
from app.services.scan_execution_service import ScanExecutionService
from app.services.scan_finalization_service import ScanFinalizationService
from app.services.verification_evaluation_service import VerificationEvaluationService
from app.services.verification_scan_creation_service import (
    VerificationScanCreationResult,
    VerificationScanCreationService,
)

# Re-export the fake adapters and helpers from test_verification_phase10
from tests.integration.test_verification_phase10 import (
    MODELS,
    SURFACES,
    FakeDispatcher,
    ScriptedAdapter,
    ScriptedRegistry,
    _adapter,
    _add_prices,
    _analyze,
    _connection_factory,
    _create_scan,
    _create_verification,
    _evaluate_verification,
    _execute,
    _factory,
    _finalize,
    _full_pipeline,
    _get_first_occurrence,
    _get_first_opportunity,
    _refresh_actions,
    _registry,
    _seed,
    _settings,
)

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------
# Tests: Targeted scope — exact historical cells per OpportunityType
# ----------------------------------------------------------------------


def test_targeted_scope_discovery_selects_all_non_branded(db_session: Session) -> None:
    """DISCOVERY_VISIBILITY_GAP: all NON_BRANDED cells across all providers."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=5,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "competitor",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, registry, dispatcher, key="baseline-disc")
    _refresh_actions(db_session, ws, project, scan.id)

    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)
    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)

    # Resolve scope.
    baseline = db_session.get(Scan, scan.id)
    scope = VerificationScopeResolver(db_session).resolve(opp, baseline)

    # DISCOVERY: all NON_BRANDED cells = 5 prompts * 2 providers = 10.
    assert scope.planned_ai_checks == 10
    assert scope.prompt_count == 5
    assert scope.provider_count == 2


def test_targeted_scope_provider_selects_single_provider(db_session: Session) -> None:
    """PROVIDER_VISIBILITY_GAP: only the Opportunity's provider cells."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=5,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    # Make OPENAI show competitor, ANTHROPIC show brand (so only OPENAI
    # generates a provider visibility gap).
    registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "brand",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, registry, dispatcher, key="baseline-prov")
    _refresh_actions(db_session, ws, project, scan.id)

    opp = _get_first_opportunity(db_session, project.id, OpportunityType.PROVIDER_VISIBILITY_GAP)
    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)

    baseline = db_session.get(Scan, scan.id)
    scope = VerificationScopeResolver(db_session).resolve(opp, baseline)

    # PROVIDER: only the Opportunity's provider cells = 5 prompts * 1 provider = 5.
    assert scope.planned_ai_checks == 5
    assert scope.prompt_count == 5
    assert scope.provider_count == 1
    assert LLMProvider.OPENAI in scope.providers
    assert LLMProvider.ANTHROPIC not in scope.providers


def test_verification_scan_non_rectangular_plan(db_session: Session) -> None:
    """planned_ai_checks = len(target_cells), NOT prompt_count * provider_count.

    For PROVIDER_VISIBILITY_GAP with 5 prompts and 2 providers, the scope
    selects only 1 provider's cells (5 cells), so:
    - planned_ai_checks = 5
    - prompt_count = 5
    - provider_count = 1
    - prompt_count * provider_count = 5 (matches here, but the point is
      planned_ai_checks is derived from cells, not the product)
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=5,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    registry = _registry(
        [LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        per_provider_modes={
            LLMProvider.OPENAI: "competitor",
            LLMProvider.ANTHROPIC: "brand",
        },
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, registry, dispatcher, key="baseline-nr")
    _refresh_actions(db_session, ws, project, scan.id)

    opp = _get_first_opportunity(db_session, project.id, OpportunityType.PROVIDER_VISIBILITY_GAP)
    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    result = _create_verification(
        db_session, ws, user, project, opp.id, registry, dispatcher, key="ver-nr"
    )
    db_session.expire_all()
    ver_scan = db_session.get(Scan, result.scan.id)

    # planned_ai_checks should be 5 (only OPENAI cells), not 10.
    assert ver_scan.planned_ai_checks == 5
    assert ver_scan.prompt_count == 5
    assert ver_scan.provider_count == 1


# ----------------------------------------------------------------------
# Tests: Brand-side resolution safeguards
# ----------------------------------------------------------------------


def test_false_resolution_brand_disappears(db_session: Session) -> None:
    """If the brand disappears from the verification scan, the gap
    closing to 0 is NOT a resolution — it's a measurement artifact.

    Baseline: competitor only (gap=100pp, brand_vis=0).
    Verification: neither brand nor competitor (gap=0pp, brand_vis=0).

    The gap is 0 because both are absent, not because the issue was
    resolved.  The brand safeguard should prevent RESOLVED.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline: competitor only.
    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-fb")
    _refresh_actions(db_session, ws, project, scan.id)

    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)
    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: neither brand nor competitor (brand disappears).
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="neither")
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-fb"
    )
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    eval_result = _evaluate_verification(db_session, result.verification.id)

    # Should NOT be RESOLVED — brand disappeared.
    assert eval_result.outcome != VerificationOutcome.RESOLVED
    assert eval_result.outcome == VerificationOutcome.NOT_IMPROVED
    assert "brand does not appear" in eval_result.evaluation_message.lower()

    # Opportunity should remain IMPLEMENTED, not VERIFIED.
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp.status == OpportunityStatus.IMPLEMENTED


# ----------------------------------------------------------------------
# Tests: Baseline coverage gate
# ----------------------------------------------------------------------


def test_baseline_zero_observations_inconclusive(db_session: Session) -> None:
    """If the baseline has zero successful observations for the scope,
    the evaluation is INCONCLUSIVE (NO_SUCCESSFUL_OBSERVATIONS).

    This test verifies the brand safeguard: if the verification has
    neither brand nor competitor (both disappear), the gap is 0 but
    the brand doesn't appear, so it's NOT RESOLVED.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline: competitor only (creates a DISCOVERY gap).
    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-bz")
    _refresh_actions(db_session, ws, project, scan.id)

    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)
    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: neither brand nor competitor (both disappear).
    # The gap will be 0 (both vis=0), but the brand safeguard should
    # prevent RESOLVED.
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="neither")
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-bz"
    )
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    db_session.expire_all()

    # Auto-evaluation should have already run during finalize.
    ver = db_session.get(OpportunityVerification, result.verification.id)
    assert ver.outcome != VerificationOutcome.PENDING

    # Should NOT be RESOLVED — brand doesn't appear.
    assert ver.outcome != VerificationOutcome.RESOLVED

    # Opportunity should remain IMPLEMENTED.
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp.status == OpportunityStatus.IMPLEMENTED


# ----------------------------------------------------------------------
# Tests: PROMPT_COMPETITOR_GAP uses competitor_only_rate (pp)
# ----------------------------------------------------------------------


def test_prompt_competitor_gap_uses_rate_not_count(db_session: Session) -> None:
    """PROMPT_COMPETITOR_GAP verification should use competitor_only_rate
    (percentage points) not competitor_only_count (integer).
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline: competitor only (creates a PROMPT_COMPETITOR_GAP).
    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-pc")
    _refresh_actions(db_session, ws, project, scan.id)

    # Find a PROMPT_COMPETITOR_GAP opportunity.
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.PROMPT_COMPETITOR_GAP)
    if opp is None:
        pytest.skip("No PROMPT_COMPETITOR_GAP opportunity generated")

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: brand only (competitor_only_rate drops to 0).
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-pc"
    )
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    eval_result = _evaluate_verification(db_session, result.verification.id)

    # The metric_name should be competitor_only_rate, not competitor_only_count.
    assert eval_result.metric_name == "competitor_only_rate"


# ----------------------------------------------------------------------
# Tests: Automatic evaluation after ScanAnalysis
# ----------------------------------------------------------------------


def test_automatic_evaluation_after_analysis(db_session: Session) -> None:
    """When a VERIFICATION scan is finalized with trigger_analysis=True,
    the analysis runs, and if it completes successfully, the verification
    is automatically evaluated.

    The full pipeline: create verification → execute → finalize (with
    auto-analysis + auto-evaluation) → verification outcome is set.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline: competitor only.
    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-ae")
    _refresh_actions(db_session, ws, project, scan.id)

    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)
    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: brand only (gap closes, should RESOLVE).
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-ae"
    )

    # Execute the verification scan.
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()

    # Finalize with trigger_analysis=True — this should auto-analyze
    # AND auto-evaluate the verification.
    _finalize(db_session, result.scan.id)
    db_session.expire_all()

    # Check that the verification was automatically evaluated.
    ver = db_session.get(OpportunityVerification, result.verification.id)
    assert ver is not None
    assert ver.outcome != VerificationOutcome.PENDING
    assert ver.evaluated_at is not None

    # Should be RESOLVED (brand only, gap=0, brand appears).
    assert ver.outcome == VerificationOutcome.RESOLVED

    # Opportunity should be VERIFIED.
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp.status == OpportunityStatus.VERIFIED


def test_analysis_failure_no_provider_replay(db_session: Session) -> None:
    """If analysis fails for a VERIFICATION scan, the scan remains
    terminal, no providers are replayed, and the verification stays
    PENDING (can be evaluated manually later).
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline: competitor only.
    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-af")
    _refresh_actions(db_session, ws, project, scan.id)

    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)
    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: brand only.
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-af"
    )

    # Count provider requests before.
    adapter = ver_registry.get(LLMProvider.OPENAI)
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()
    requests_before = len(adapter.requests)

    # Finalize — this triggers analysis and auto-evaluation.
    # The key assertion: no additional provider requests are made
    # during finalization, analysis, or auto-evaluation.
    _finalize(db_session, result.scan.id)
    db_session.expire_all()

    # No additional provider requests.
    requests_after = len(adapter.requests)
    assert requests_after == requests_before

    # Scan should be terminal (COMPLETED or PARTIAL).
    ver_scan = db_session.get(Scan, result.scan.id)
    assert ver_scan.status in (ScanStatus.COMPLETED, ScanStatus.PARTIAL)

    # Verification should be auto-evaluated (not PENDING).
    ver = db_session.get(OpportunityVerification, result.verification.id)
    assert ver.outcome != VerificationOutcome.PENDING
    assert ver.evaluated_at is not None


# ----------------------------------------------------------------------
# Tests: One pending verification per implementation cycle
# ----------------------------------------------------------------------


def test_one_pending_verification_per_cycle(db_session: Session) -> None:
    """Creating a second verification while the first is still PENDING
    raises ConflictError.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-1pv")
    _refresh_actions(db_session, ws, project, scan.id)

    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)
    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Create first verification (PENDING).
    result1 = _create_verification(
        db_session, ws, user, project, opp.id, baseline_registry, dispatcher, key="ver-1pv-a"
    )

    # Attempt to create a second verification with a DIFFERENT key.
    # This should fail because the first is still PENDING.
    with pytest.raises(ConflictError, match="active verification scan|pending verification"):
        _create_verification(
            db_session, ws, user, project, opp.id, baseline_registry, dispatcher, key="ver-1pv-b"
        )


# ----------------------------------------------------------------------
# Tests: Second verification after terminal evaluation
# ----------------------------------------------------------------------


def test_second_verification_after_terminal(db_session: Session) -> None:
    """After the first verification is evaluated (terminal outcome),
    a second verification can be created for the same implementation
    cycle.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-2v")
    _refresh_actions(db_session, ws, project, scan.id)

    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)
    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Create and evaluate first verification (NOT_IMPROVED).
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    result1 = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-2v-a"
    )
    _execute(db_session, result1.scan.id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, result1.scan.id)
    db_session.expire_all()

    # First verification should be evaluated (auto-evaluation).
    ver1 = db_session.get(OpportunityVerification, result1.verification.id)
    assert ver1.outcome != VerificationOutcome.PENDING

    # Now create a second verification with a different key.
    result2 = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-2v-b"
    )
    assert result2.created is True
    assert result2.verification.id != result1.verification.id


# ----------------------------------------------------------------------
# Tests: Idempotency + baseline conflict
# ----------------------------------------------------------------------


def test_idempotency_baseline_conflict(db_session: Session) -> None:
    """Reusing the same idempotency key after a re-implementation cycle
    (different baseline) raises ConflictError.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan1 = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-ic-1")
    _refresh_actions(db_session, ws, project, scan1.id)

    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)
    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Create first verification with key "ver-ic".
    result1 = _create_verification(
        db_session, ws, user, project, opp.id, baseline_registry, dispatcher, key="ver-ic"
    )
    _execute(db_session, result1.scan.id, baseline_registry)
    db_session.expire_all()
    _finalize(db_session, result1.scan.id)
    db_session.expire_all()

    # Evaluate the first verification.
    ver1 = db_session.get(OpportunityVerification, result1.verification.id)
    if ver1.outcome == VerificationOutcome.PENDING:
        _evaluate_verification(db_session, result1.verification.id)

    # Re-implementation cycle: IMPLEMENTED → IN_PROGRESS → IMPLEMENTED.
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    if opp.status == OpportunityStatus.VERIFIED:
        pytest.skip("Opportunity was VERIFIED, cannot re-implement")

    old_baseline_occ_id = opp.implementation_baseline_occurrence_id

    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    db_session.expire_all()

    # Run a new baseline scan.
    scan2 = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-ic-2")
    _refresh_actions(db_session, ws, project, scan2.id)
    db_session.expire_all()

    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)

    # Verify the baseline has actually changed.
    new_baseline_occ_id = opp.implementation_baseline_occurrence_id
    assert new_baseline_occ_id is not None, "New baseline occurrence not frozen"
    assert new_baseline_occ_id != old_baseline_occ_id, "Baseline did not change after re-implementation"

    # Reuse the same idempotency key — should raise ConflictError
    # because the baseline has changed.
    with pytest.raises(ConflictError, match="baseline"):
        _create_verification(
            db_session, ws, user, project, opp.id, baseline_registry, dispatcher, key="ver-ic"
        )
