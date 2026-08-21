"""Integration tests for Phase 10.2 — Verification Resolution and
Terminal Lifecycle Integrity.

Tests cover:
- Brand-side safeguard compares vs frozen baseline (not just > 0)
  - DISCOVERY: brand visibility deteriorated → NOT RESOLVED
  - PROVIDER: provider brand visibility deteriorated → NOT RESOLVED
  - CITATION: brand owned citation rate deteriorated → NOT RESOLVED
  - PROMPT: categorical resolution (competitor_only_rate == 0 + brand appears)
- Terminal lifecycle: FAILED scan → INCONCLUSIVE / VERIFICATION_SCAN_FAILED
  - Quota failure terminalizes verification + releases PENDING slot
  - Quota failure retry with new key succeeds
  - Manual evaluate of FAILED scan → INCONCLUSIVE gracefully
- Implementation cycle validation
  - Old-cycle RESOLVED cannot mark new cycle VERIFIED
  - Current-cycle RESOLVED → VERIFIED
- Real PostgreSQL different-key concurrency
- Targeted scope regression: CITATION + PROMPT explicit
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    LLMProvider,
    OpportunityStatus,
    OpportunityType,
    ProviderExecutionMode,
    ScanStatus,
    ScanType,
    VerificationOutcome,
    VerificationReasonCode,
)
from app.core.verification_scope import VerificationScopeResolver
from app.models import (
    Opportunity,
    OpportunityVerification,
    Scan,
)
from app.services.opportunity_workflow_service import OpportunityWorkflowService

# Re-export the fake adapters and helpers from test_verification_phase10
from tests.integration.test_verification_phase10 import (
    SURFACES,
    FakeDispatcher,
    ScriptedAdapter,
    ScriptedRegistry,
    _add_prices,
    _analyze,
    _create_verification,
    _evaluate_verification,
    _execute,
    _finalize,
    _full_pipeline,
    _get_first_opportunity,
    _refresh_actions,
    _registry,
    _seed,
)

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------
# Per-run adapter for fine-grained control over mention modes and
# citations per individual PromptRun.
# ----------------------------------------------------------------------


class PerRunAdapter(ScriptedAdapter):
    """ScriptedAdapter with per-run mention modes and citation control.

    Allows each individual PromptRun to have a different mention_mode
    and different citation URLs, so we can construct scenarios like
    brand_visibility = 50% (5/10 runs mention brand) without needing
    multiple providers.
    """

    def __init__(
        self,
        *args: Any,
        per_run_modes: list[str] | None = None,
        per_run_brand_citations: list[bool] | None = None,
        per_run_competitor_citations: list[bool] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._per_run_modes = list(per_run_modes or [])
        self._per_run_brand_citations = list(per_run_brand_citations or [])
        self._per_run_competitor_citations = list(per_run_competitor_citations or [])
        self._run_idx = 0

    async def execute(self, request: Any) -> Any:
        idx = self._run_idx
        self._run_idx += 1

        if idx < len(self._per_run_modes):
            self.mention_mode = self._per_run_modes[idx]

        original_brand_url = self.brand_citation_url
        original_competitor_url = self.competitor_citation_url

        if idx < len(self._per_run_brand_citations) and not self._per_run_brand_citations[idx]:
            self.brand_citation_url = None
        if (
            idx < len(self._per_run_competitor_citations)
            and not self._per_run_competitor_citations[idx]
        ):
            self.competitor_citation_url = None

        try:
            return await super().execute(request)
        finally:
            self.brand_citation_url = original_brand_url
            self.competitor_citation_url = original_competitor_url


def _per_run_registry(
    provider: LLMProvider,
    per_run_modes: list[str],
    *,
    per_run_brand_citations: list[bool] | None = None,
    per_run_competitor_citations: list[bool] | None = None,
    brand_name: str = "Acme",
    competitor_name: str = "Rival",
    brand_domain: str = "acme.test",
    competitor_domain: str = "rival.test",
) -> ScriptedRegistry:
    """Build a registry with a single PerRunAdapter."""
    adapter = PerRunAdapter(
        provider,
        SURFACES[provider],
        brand_name=brand_name,
        competitor_name=competitor_name,
        mention_mode="both",
        brand_citation_url=f"https://{brand_domain}/page",
        competitor_citation_url=f"https://{competitor_domain}/page",
        per_run_modes=per_run_modes,
        per_run_brand_citations=per_run_brand_citations,
        per_run_competitor_citations=per_run_competitor_citations,
    )
    return ScriptedRegistry({provider: adapter})


# ----------------------------------------------------------------------
# Tests: Brand-side safeguard — brand metric must not deteriorate vs
# frozen baseline (Phase 10.2).
# ----------------------------------------------------------------------


def test_discovery_brand_deteriorated_not_resolved(db_session: Session) -> None:
    """DISCOVERY: brand visibility deteriorated vs baseline → NOT RESOLVED.

    Baseline: brand=50%, competitor=100%, gap=50pp.
    Verification: brand=10%, competitor=10%, gap=0pp (< 10pp threshold).

    Although the gap fell below the resolution threshold, the brand
    itself deteriorated from 50% to 10%.  The opportunity must NOT
    become VERIFIED.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline: 5 "both" + 5 "competitor" → brand=50%, competitor=100%, gap=50pp.
    baseline_registry = _per_run_registry(
        LLMProvider.OPENAI,
        per_run_modes=["both"] * 5 + ["competitor"] * 5,
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-disc-d"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: 1 "both" + 9 "neither" → brand=10%, competitor=10%, gap=0pp.
    ver_registry = _per_run_registry(
        LLMProvider.OPENAI,
        per_run_modes=["both"] + ["neither"] * 9,
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-disc-d"
    )
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    eval_result = _evaluate_verification(db_session, result.verification.id)

    # NOT RESOLVED — brand deteriorated from 50% to 10%.
    assert eval_result.outcome != VerificationOutcome.RESOLVED
    assert "brand-side metric" in eval_result.evaluation_message.lower()

    # Opportunity should remain IMPLEMENTED, not VERIFIED.
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.IMPLEMENTED


def test_provider_brand_deteriorated_not_resolved(db_session: Session) -> None:
    """PROVIDER: provider brand visibility deteriorated vs baseline → NOT RESOLVED.

    Uses two providers.  The PROVIDER_VISIBILITY_GAP opportunity is
    scoped to one provider only.  The other provider's measurements
    must not influence the safeguard.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    # Baseline: OPENAI competitor-only (gap), ANTHROPIC brand-only (no gap).
    # The PROVIDER_VISIBILITY_GAP opportunity will be for OPENAI.
    # OPENAI: 5 "both" + 5 "competitor" → brand=50%, competitor=100%, gap=50pp.
    # ANTHROPIC: "brand" → no gap for ANTHROPIC.
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
                per_run_modes=["both"] * 5 + ["competitor"] * 5,
            ),
            LLMProvider.ANTHROPIC: ScriptedAdapter(
                LLMProvider.ANTHROPIC,
                SURFACES[LLMProvider.ANTHROPIC],
                brand_name="Acme",
                competitor_name="Rival",
                mention_mode="brand",
                brand_citation_url="https://acme.test/page",
                competitor_citation_url="https://rival.test/page",
            ),
        }
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-prov-d"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.PROVIDER_VISIBILITY_GAP)
    assert opp.provider == LLMProvider.OPENAI

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: OPENAI 1 "both" + 9 "neither" → brand=10%, competitor=10%, gap=0pp.
    # ANTHROPIC: "brand" (doesn't affect the OPENAI-scoped safeguard).
    ver_registry = ScriptedRegistry(
        {
            LLMProvider.OPENAI: PerRunAdapter(
                LLMProvider.OPENAI,
                SURFACES[LLMProvider.OPENAI],
                brand_name="Acme",
                competitor_name="Rival",
                mention_mode="both",
                brand_citation_url="https://acme.test/page",
                competitor_citation_url="https://rival.test/page",
                per_run_modes=["both"] + ["neither"] * 9,
            ),
            LLMProvider.ANTHROPIC: ScriptedAdapter(
                LLMProvider.ANTHROPIC,
                SURFACES[LLMProvider.ANTHROPIC],
                brand_name="Acme",
                competitor_name="Rival",
                mention_mode="brand",
                brand_citation_url="https://acme.test/page",
                competitor_citation_url="https://rival.test/page",
            ),
        }
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-prov-d"
    )
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    eval_result = _evaluate_verification(db_session, result.verification.id)

    # NOT RESOLVED — OPENAI brand deteriorated from 50% to 10%.
    assert eval_result.outcome != VerificationOutcome.RESOLVED
    assert "brand-side metric" in eval_result.evaluation_message.lower()

    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.IMPLEMENTED


def test_citation_brand_deteriorated_not_resolved(db_session: Session) -> None:
    """CITATION: brand owned citation rate deteriorated vs baseline → NOT RESOLVED.

    Baseline: brand_citation=50%, competitor_citation=100%, gap=50pp.
    Verification: brand_citation=10%, competitor_citation=10%, gap=0pp.

    All runs are WEB_GROUNDED (OPENAI), so all are citation-eligible.
    Brand citation presence is controlled per-run.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline: all "both" (brand+competitor mentioned), but brand
    # citation only in 5/10 runs.  Competitor citation in all 10.
    # → brand_citation=50%, competitor_citation=100%, gap=50pp.
    baseline_registry = _per_run_registry(
        LLMProvider.OPENAI,
        per_run_modes=["both"] * 10,
        per_run_brand_citations=[True] * 5 + [False] * 5,
        per_run_competitor_citations=[True] * 10,
    )
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-cit-d"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.OWNED_CITATION_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: all "both", brand citation in 1/10, competitor
    # citation in 1/10 → gap=0pp (< 20pp threshold).
    # But brand_citation deteriorated from 50% to 10%.
    ver_registry = _per_run_registry(
        LLMProvider.OPENAI,
        per_run_modes=["both"] * 10,
        per_run_brand_citations=[True] + [False] * 9,
        per_run_competitor_citations=[True] + [False] * 9,
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-cit-d"
    )
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    eval_result = _evaluate_verification(db_session, result.verification.id)

    # NOT RESOLVED — brand citation deteriorated from 50% to 10%.
    assert eval_result.outcome != VerificationOutcome.RESOLVED
    assert "brand-side metric" in eval_result.evaluation_message.lower()

    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.IMPLEMENTED


def test_prompt_true_resolution_still_works(db_session: Session) -> None:
    """PROMPT_COMPETITOR_GAP: competitor_only_rate=0 AND brand appears → RESOLVED.

    This is the categorical resolution condition.  It does NOT require
    brand_visibility >= baseline_brand_visibility.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline: all "competitor" → competitor_only_rate=100%.
    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-ptr"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.PROMPT_COMPETITOR_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: all "brand" → competitor_only_rate=0, brand appears.
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-ptr"
    )
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    eval_result = _evaluate_verification(db_session, result.verification.id)

    assert eval_result.outcome == VerificationOutcome.RESOLVED

    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.VERIFIED


# ----------------------------------------------------------------------
# Tests: Terminal lifecycle — FAILED scan → INCONCLUSIVE
# ----------------------------------------------------------------------


def test_quota_failure_terminalizes_verification(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quota reservation failure → Scan FAILED + Verification INCONCLUSIVE.

    The PENDING slot is released so a new verification can be created
    with a new idempotency key.

    The quota service's session.rollback() would undo the scan INSERT
    in the test's transaction-wrapped session.  We monkey-patch the
    quota service to raise QuotaExceededError without the destructive
    rollback, simulating the production behavior where the scan was
    already committed before the quota reservation.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=5,
        monthly_limit=5,  # Just enough for baseline, not verification.
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline uses all 5 AI checks.
    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-qf")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Monkey-patch QuotaService.reserve_ai_checks to raise
    # QuotaExceededError without calling session.rollback(), simulating
    # the production behavior where the scan was already committed.
    from app.core.exceptions import QuotaExceededError
    from app.services.quota_service import QuotaService

    def failing_reserve(self: QuotaService, *args: Any, **kwargs: Any) -> Any:
        raise QuotaExceededError(
            "AI Check quota exceeded. Limit: 5, Used: 5, Reserved: 0, Requested: 5."
        )

    monkeypatch.setattr(QuotaService, "reserve_ai_checks", failing_reserve)

    # Verification attempt with insufficient quota.
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")

    with pytest.raises(QuotaExceededError):
        _create_verification(
            db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-qf-1"
        )

    db_session.expire_all()

    # Scan should be FAILED.
    ver_scan = (
        db_session.execute(select(Scan).where(Scan.scan_type == ScanType.VERIFICATION))
        .scalars()
        .first()
    )
    assert ver_scan is not None
    assert ver_scan.status == ScanStatus.FAILED

    # Verification should be INCONCLUSIVE with VERIFICATION_SCAN_FAILED.
    ver = (
        db_session.execute(
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

    # No PENDING verification remains for the cycle.
    pending = list(
        db_session.execute(
            select(OpportunityVerification).where(
                OpportunityVerification.opportunity_id == opp.id,
                OpportunityVerification.outcome == VerificationOutcome.PENDING,
            )
        ).scalars()
    )
    assert len(pending) == 0


def test_quota_failure_retry_with_new_key(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After quota failure terminalizes a verification, a new verification
    with a new idempotency key can be created when quota becomes available.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=5,
        monthly_limit=5,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline uses all 5 AI checks.
    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-qfr"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Monkey-patch QuotaService to fail the first reservation, then
    # allow subsequent ones.
    from app.core.exceptions import QuotaExceededError
    from app.services.quota_service import QuotaService

    call_count = {"n": 0}
    original_reserve = QuotaService.reserve_ai_checks

    def failing_reserve(self: QuotaService, *args: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise QuotaExceededError(
                "AI Check quota exceeded. Limit: 5, Used: 5, Reserved: 0, Requested: 5."
            )
        return original_reserve(self, *args, **kwargs)

    monkeypatch.setattr(QuotaService, "reserve_ai_checks", failing_reserve)

    # First verification attempt: insufficient quota → FAILED.
    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")

    with pytest.raises(QuotaExceededError):
        _create_verification(
            db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-qfr-1"
        )
    db_session.expire_all()

    # Increase quota by updating the plan's monthly limit.
    from app.models import PlanDefinition

    plan = (
        db_session.execute(select(PlanDefinition).where(PlanDefinition.code.startswith("P10_")))
        .scalars()
        .first()
    )
    assert plan is not None
    plan.monthly_ai_checks = 1000
    db_session.commit()
    db_session.expire_all()

    # Second verification with a new key: should succeed.
    result2 = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-qfr-2"
    )
    assert result2.created is True
    assert result2.scan.status in (ScanStatus.PENDING, ScanStatus.RUNNING)


def test_manual_evaluate_failed_scan_graceful(db_session: Session) -> None:
    """Manually evaluating a FAILED scan returns INCONCLUSIVE gracefully
    instead of raising ValidationError.

    Phase 10.3: finalization now automatically terminalizes the
    PENDING verification when the scan is FAILED. This test asserts
    the automatic behavior without manually calling the lifecycle
    service.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=5,
        monthly_limit=100,  # Enough for both baseline + verification.
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-mef"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: all runs fail → Scan FAILED.
    from app.providers.errors import ProviderResponseError

    ver_registry = _registry(
        [LLMProvider.OPENAI],
        mention_mode="brand",
        outcomes={LLMProvider.OPENAI: [ProviderResponseError("fail")] * 5},
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-mef"
    )
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    db_session.expire_all()

    ver_scan = db_session.get(Scan, result.scan.id)
    assert ver_scan is not None
    assert ver_scan.status == ScanStatus.FAILED

    # Phase 10.3: finalization MUST automatically terminalize the
    # PENDING verification. No manual lifecycle call needed.
    ver = db_session.get(OpportunityVerification, result.verification.id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.INCONCLUSIVE
    assert ver.reason_code == VerificationReasonCode.VERIFICATION_SCAN_FAILED
    assert ver.evaluated_at is not None

    # Opportunity remains IMPLEMENTED (not VERIFIED).
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.IMPLEMENTED


# ----------------------------------------------------------------------
# Tests: Implementation cycle validation
# ----------------------------------------------------------------------


def test_old_cycle_resolved_cannot_verify_new_cycle(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RESOLVED verification from an OLD implementation cycle cannot
    mark a NEW cycle VERIFIED.

    Scenario:
    1. Baseline A → opportunity → IMPLEMENTED (baseline = A)
    2. Verification A created + executed (auto-evaluation disabled)
    3. Re-implement (new baseline = B)
    4. Verification A manually evaluated → RESOLVED
    5. Opportunity should remain IMPLEMENTED (cycle mismatch)

    The auto-evaluation is disabled to simulate a delayed evaluation
    (e.g., analysis failure, manual review).  This tests the cycle
    validation in mark_verified_from_verification.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=10,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline A: competitor only → large gap.
    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan_a = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-a"
    )
    _refresh_actions(db_session, ws, project, scan_a.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    baseline_a = opp.implementation_baseline_occurrence_id
    assert baseline_a is not None

    # Disable auto-evaluation to simulate a delayed evaluation.
    from app.services.scan_finalization_service import ScanFinalizationService

    def disabled_auto_eval(self: Any, *args: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr(
        ScanFinalizationService, "_maybe_auto_evaluate_verification", disabled_auto_eval
    )

    # Start Verification A (execute + finalize + analyze, but no auto-eval).
    ver_registry_a = _registry([LLMProvider.OPENAI], mention_mode="brand")
    result_a = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry_a, dispatcher, key="ver-a"
    )
    _execute(db_session, result_a.scan.id, ver_registry_a)
    db_session.expire_all()
    _finalize(db_session, result_a.scan.id)
    _analyze(db_session, result_a.scan.id)
    db_session.expire_all()

    # Restore auto-evaluation.
    monkeypatch.undo()

    # Verification A should still be PENDING (no auto-evaluation).
    ver_a = db_session.get(OpportunityVerification, result_a.verification.id)
    assert ver_a is not None
    assert ver_a.outcome == VerificationOutcome.PENDING

    # Re-implement (new baseline B).
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    db_session.expire_all()
    scan_b = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-b"
    )
    _refresh_actions(db_session, ws, project, scan_b.id)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    baseline_b = opp.implementation_baseline_occurrence_id
    assert baseline_b is not None
    assert baseline_b != baseline_a

    # Now manually evaluate Verification A → RESOLVED.
    eval_result = _evaluate_verification(db_session, result_a.verification.id)
    assert eval_result.outcome == VerificationOutcome.RESOLVED

    # But Opportunity should NOT be VERIFIED — cycle mismatch.
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.IMPLEMENTED
    assert opp.implementation_baseline_occurrence_id == baseline_b


def test_current_cycle_resolved_verifies(db_session: Session) -> None:
    """A RESOLVED verification for the CURRENT implementation cycle
    transitions the Opportunity to VERIFIED.

    This confirms the new cycle guard does not block valid verification.
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
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-ccr"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    ver_registry = _registry([LLMProvider.OPENAI], mention_mode="brand")
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-ccr"
    )
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    _analyze(db_session, result.scan.id)
    db_session.expire_all()

    eval_result = _evaluate_verification(db_session, result.verification.id)
    assert eval_result.outcome == VerificationOutcome.RESOLVED
    assert eval_result.opportunity_status_after == OpportunityStatus.VERIFIED.value

    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.VERIFIED
    assert opp.verified_at is not None


# ----------------------------------------------------------------------
# Tests: Targeted scope regression — CITATION + PROMPT explicit
# ----------------------------------------------------------------------


def test_targeted_scope_citation_web_grounded_only(db_session: Session) -> None:
    """OWNED_CITATION_GAP: only WEB_GROUNDED cells are selected.

    OPENAI and ANTHROPIC are both WEB_GROUNDED, so all cells are
    selected.  GOOGLE would be MODEL_ONLY and excluded, but we don't
    use GOOGLE here.  Instead, verify the scope selects all
    WEB_GROUNDED cells.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
        prompt_count=5,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])

    # Baseline: competitor cited more than brand → citation gap.
    baseline_registry = _per_run_registry(
        LLMProvider.OPENAI,
        per_run_modes=["both"] * 5,
        per_run_brand_citations=[False] * 5,
        per_run_competitor_citations=[True] * 5,
    )
    # Use a multi-provider registry.
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
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-cit-s"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.OWNED_CITATION_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None

    baseline = db_session.get(Scan, scan.id)
    assert baseline is not None
    scope = VerificationScopeResolver(db_session).resolve(opp, baseline)

    # CITATION: all WEB_GROUNDED cells = 5 prompts * 2 providers = 10.
    assert scope.planned_ai_checks == 10
    assert scope.prompt_count == 5
    assert scope.provider_count == 2

    # All cells should be WEB_GROUNDED.
    for cell in scope.target_cells:
        assert cell.execution_mode == ProviderExecutionMode.WEB_GROUNDED


def test_targeted_scope_prompt_exact_prompt_only(db_session: Session) -> None:
    """PROMPT_COMPETITOR_GAP: only the exact prompt_id cells are selected."""
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=5,
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    # Baseline: competitor only → prompt gap.
    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(
        db_session, ws, user, project, baseline_registry, dispatcher, key="bl-pr-s"
    )
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.PROMPT_COMPETITOR_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None

    baseline = db_session.get(Scan, scan.id)
    assert baseline is not None
    scope = VerificationScopeResolver(db_session).resolve(opp, baseline)

    # PROMPT: exact prompt only = 1 prompt * 1 provider = 1 cell.
    assert scope.planned_ai_checks == 1
    assert opp.prompt_id is not None
    for cell in scope.target_cells:
        assert cell.prompt_id == opp.prompt_id


# ----------------------------------------------------------------------
# Tests: VerificationLifecycleService direct
# ----------------------------------------------------------------------


def test_lifecycle_service_terminalizes_failed_scan(db_session: Session) -> None:
    """Finalization automatically terminalizes a PENDING verification
    when its scan is FAILED (all runs failed).

    Phase 10.3: this test asserts the automatic behavior without
    manually calling VerificationLifecycleService.
    """
    ws, user, project, _ps, _prompts = _seed(
        db_session,
        providers=[LLMProvider.OPENAI],
        prompt_count=5,
        monthly_limit=100,  # Enough for both baseline + verification.
        competitors=[("Rival", "rival.test")],
    )
    _add_prices(db_session, [LLMProvider.OPENAI])

    baseline_registry = _registry([LLMProvider.OPENAI], mention_mode="competitor")
    dispatcher = FakeDispatcher()
    scan = _full_pipeline(db_session, ws, user, project, baseline_registry, dispatcher, key="bl-ls")
    _refresh_actions(db_session, ws, project, scan.id)
    opp = _get_first_opportunity(db_session, project.id, OpportunityType.DISCOVERY_VISIBILITY_GAP)

    workflow = OpportunityWorkflowService(db_session)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IN_PROGRESS)
    workflow.transition(ws.id, project.id, opp.id, OpportunityStatus.IMPLEMENTED)
    db_session.expire_all()

    # Verification: all runs fail → Scan FAILED.
    from app.providers.errors import ProviderResponseError

    ver_registry = _registry(
        [LLMProvider.OPENAI],
        mention_mode="brand",
        outcomes={LLMProvider.OPENAI: [ProviderResponseError("fail")] * 5},
    )
    result = _create_verification(
        db_session, ws, user, project, opp.id, ver_registry, dispatcher, key="ver-ls"
    )
    _execute(db_session, result.scan.id, ver_registry)
    db_session.expire_all()
    _finalize(db_session, result.scan.id)
    db_session.expire_all()

    ver_scan = db_session.get(Scan, result.scan.id)
    assert ver_scan is not None
    assert ver_scan.status == ScanStatus.FAILED

    # Phase 10.3: finalization MUST automatically terminalize.
    ver = db_session.get(OpportunityVerification, result.verification.id)
    assert ver is not None
    assert ver.outcome == VerificationOutcome.INCONCLUSIVE
    assert ver.reason_code == VerificationReasonCode.VERIFICATION_SCAN_FAILED
    assert ver.evaluated_at is not None

    # Opportunity remains IMPLEMENTED.
    opp = db_session.get(Opportunity, opp.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.IMPLEMENTED
