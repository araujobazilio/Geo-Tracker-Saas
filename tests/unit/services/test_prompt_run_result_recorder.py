"""Unit tests for PromptRunResultRecorder failure evidence path (Phase 13.5.10).

These tests mock the SQLAlchemy session and repositories to verify the
recorder's failure-evidence logic without requiring a real database.

Coverage:
- record_failure_evidence persists IDs, usage, cost, UsageEvent, FAILED status
- Idempotency: double call does not create two UsageEvents
- Idempotency: already-SUCCEEDED run is a no-op
- Contract validation rejects mismatched evidence
- Case A (pre-provider) uses _record_failure, not record_failure_evidence
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.core.enums import (
    CostSource,
    LLMProvider,
    PromptRunStatus,
    ProviderErrorCode,
    ProviderExecutionMode,
    ProviderSurface,
)
from app.core.exceptions import ConflictError
from app.models.scan import PromptRun, ResponseSource, Scan
from app.providers.base import ProviderCitation, ProviderFailureEvidence, ProviderUsage
from app.providers.errors import ProviderResponseError
from app.services.pricing_service import CostComputation
from app.services.prompt_run_result_recorder import PromptRunResultRecorder


def _make_evidence(
    *,
    provider: LLMProvider = LLMProvider.OPENAI,
    surface: ProviderSurface = ProviderSurface.OPENAI_RESPONSES_API,
    mode: ProviderExecutionMode = ProviderExecutionMode.WEB_GROUNDED,
    requested_model: str = "gpt-5.6-terra",
    returned_model: str | None = "gpt-5.6-terra",
    provider_request_id: str | None = "req_ev_001",
    provider_response_id: str | None = "resp_ev_001",
    usage: ProviderUsage | None = None,
    citations: tuple[ProviderCitation, ...] = (),
    latency_ms: int = 42,
    search_used: bool = True,
    incomplete_reason: str | None = "max_output_tokens",
    max_tool_calls_violation: int | None = None,
    requested_max_tool_calls: int | None = None,
    observed_search_requests: int | None = None,
) -> ProviderFailureEvidence:
    return ProviderFailureEvidence(
        provider=provider,
        surface=surface,
        execution_mode=mode,
        requested_model=requested_model,
        returned_model=returned_model,
        provider_request_id=provider_request_id,
        provider_response_id=provider_response_id,
        usage=usage
        or ProviderUsage(
            input_tokens=100,
            output_tokens=0,
            total_tokens=100,
            cached_input_tokens=20,
            reasoning_tokens=0,
            search_requests=1,
        ),
        citations=citations,
        latency_ms=latency_ms,
        search_used=search_used,
        incomplete_reason=incomplete_reason,
        max_tool_calls_violation=max_tool_calls_violation,
        requested_max_tool_calls=requested_max_tool_calls,
        observed_search_requests=observed_search_requests,
    )


def _make_run(
    *,
    status: PromptRunStatus = PromptRunStatus.RUNNING,
    usage_event_id: uuid.UUID | None = None,
    provider: LLMProvider = LLMProvider.OPENAI,
    surface: ProviderSurface = ProviderSurface.OPENAI_RESPONSES_API,
    mode: ProviderExecutionMode = ProviderExecutionMode.WEB_GROUNDED,
    requested_model: str = "gpt-5.6-terra",
) -> PromptRun:
    run = MagicMock(spec=PromptRun)
    run.id = uuid.uuid4()
    run.scan_id = uuid.uuid4()
    run.status = status
    run.usage_event_id = usage_event_id
    run.provider = provider.value
    run.provider_surface = surface.value
    run.execution_mode = mode.value
    run.requested_model = requested_model
    run.started_at = datetime.now(UTC)
    return run


_UNSET = object()


def _make_scan(*, quota_reservation_id: uuid.UUID | None | object = _UNSET) -> Scan:
    scan = MagicMock(spec=Scan)
    scan.id = uuid.uuid4()
    if quota_reservation_id is _UNSET:
        scan.quota_reservation_id = uuid.uuid4()
    else:
        scan.quota_reservation_id = quota_reservation_id
    return scan


class _MockSession:
    """Minimal SQLAlchemy session mock that tracks commit/rollback."""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class _MockUsageEvent:
    def __init__(self) -> None:
        self.id = uuid.uuid4()


def _setup_recorder_mocks(
    session: _MockSession,
    *,
    run: PromptRun,
    scan: Scan,
    cost: CostComputation | None = None,
) -> tuple[PromptRunResultRecorder, MagicMock, MagicMock, MagicMock, MagicMock, MagicMock]:
    """Wire up all dependencies of PromptRunResultRecorder with mocks."""
    runs_repo = MagicMock()
    runs_repo.get_for_update.return_value = run
    scans_repo = MagicMock()
    scans_repo.get_by_id.return_value = scan
    sources_repo = MagicMock()
    pricing = MagicMock()
    if cost is not None:
        pricing.resolve_optional.return_value = MagicMock(id=uuid.uuid4())
    calculator = MagicMock()
    calculator.calculate_failure.return_value = cost or CostComputation(
        cost_usd=Decimal("0.0012"),
        calculated_cost_usd=Decimal("0.0012"),
        provider_reported_cost_usd=None,
        source=CostSource.PRICE_RULE,
        complete=True,
        pricing_rule_id=uuid.uuid4(),
    )
    quota = MagicMock()
    quota.commit_ai_checks.return_value = _MockUsageEvent()

    recorder = PromptRunResultRecorder(session)  # type: ignore[arg-type]
    recorder._runs = runs_repo
    recorder._scans = scans_repo
    recorder._sources = sources_repo
    recorder._pricing = pricing
    recorder._calculator = calculator
    recorder._quota = quota

    return recorder, runs_repo, scans_repo, sources_repo, calculator, quota


def test_record_failure_evidence_persists_all_fields() -> None:
    """Case B: evidence with usage -> FAILED with IDs, usage, cost, UsageEvent."""
    session = _MockSession()
    run = _make_run()
    scan = _make_scan()
    evidence = _make_evidence()

    recorder, runs_repo, _, sources_repo, _, quota = _setup_recorder_mocks(
        session, run=run, scan=scan
    )

    recorder.record_failure_evidence(
        run.id,
        evidence,
        error_code=ProviderErrorCode.MALFORMED_RESPONSE,
        error_message="empty text",
    )

    assert session.committed is True
    assert session.rolled_back is False
    # Run is marked FAILED
    assert run.status == PromptRunStatus.FAILED
    assert run.error_code == ProviderErrorCode.MALFORMED_RESPONSE
    assert run.error_message == "empty text"
    # IDs preserved
    assert run.provider_request_id == "req_ev_001"
    assert run.provider_response_id == "resp_ev_001"
    # Usage preserved
    assert run.input_tokens == 100
    assert run.output_tokens == 0
    assert run.cached_input_tokens == 20
    assert run.search_requests == 1
    # Cost preserved
    assert run.cost_usd == Decimal("0.0012")
    assert run.cost_source == CostSource.PRICE_RULE
    # UsageEvent linked
    assert run.usage_event_id is not None
    # Quota committed
    quota.commit_ai_checks.assert_called_once()
    call_kwargs = quota.commit_ai_checks.call_args.kwargs
    assert call_kwargs["quantity"] == 1
    assert call_kwargs["commit_transaction"] is False
    # Citations persisted
    sources_repo.create_batch_idempotent.assert_called_once()


def test_record_failure_evidence_idempotent_already_failed() -> None:
    """Double call: already FAILED with usage_event_id -> no-op."""
    session = _MockSession()
    existing_event_id = uuid.uuid4()
    run = _make_run(status=PromptRunStatus.FAILED, usage_event_id=existing_event_id)
    scan = _make_scan()
    evidence = _make_evidence()

    recorder, runs_repo, _, sources_repo, _, quota = _setup_recorder_mocks(
        session, run=run, scan=scan
    )

    recorder.record_failure_evidence(
        run.id,
        evidence,
        error_code=ProviderErrorCode.MALFORMED_RESPONSE,
        error_message="empty text",
    )

    assert session.committed is True
    # No quota commit, no sources, no calculator
    quota.commit_ai_checks.assert_not_called()
    sources_repo.create_batch_idempotent.assert_not_called()
    # run fields NOT overwritten
    assert run.error_code != ProviderErrorCode.MALFORMED_RESPONSE


def test_record_failure_evidence_idempotent_already_succeeded() -> None:
    """Already SUCCEEDED -> no-op (commit and return)."""
    session = _MockSession()
    run = _make_run(status=PromptRunStatus.SUCCEEDED)
    scan = _make_scan()
    evidence = _make_evidence()

    recorder, _, _, sources_repo, _, quota = _setup_recorder_mocks(session, run=run, scan=scan)

    recorder.record_failure_evidence(
        run.id,
        evidence,
        error_code=ProviderErrorCode.MALFORMED_RESPONSE,
        error_message="empty text",
    )

    assert session.committed is True
    quota.commit_ai_checks.assert_not_called()
    sources_repo.create_batch_idempotent.assert_not_called()


def test_record_failure_evidence_rejects_mismatched_contract() -> None:
    """Evidence provider/surface/mode/model must match the PromptRun."""
    session = _MockSession()
    run = _make_run(provider=LLMProvider.ANTHROPIC)  # different provider
    scan = _make_scan()
    evidence = _make_evidence(provider=LLMProvider.OPENAI)  # mismatch

    recorder, _, _, _, _, _ = _setup_recorder_mocks(session, run=run, scan=scan)

    with pytest.raises(ProviderResponseError):
        recorder.record_failure_evidence(
            run.id, evidence, error_code=ProviderErrorCode.MALFORMED_RESPONSE, error_message="empty"
        )

    assert session.rolled_back is True


def test_record_failure_evidence_rejects_non_running_run() -> None:
    """Run in PENDING state -> ConflictError."""
    session = _MockSession()
    run = _make_run(status=PromptRunStatus.PENDING)
    scan = _make_scan()
    evidence = _make_evidence()

    recorder, _, _, _, _, _ = _setup_recorder_mocks(session, run=run, scan=scan)

    with pytest.raises(ConflictError):
        recorder.record_failure_evidence(
            run.id, evidence, error_code=ProviderErrorCode.MALFORMED_RESPONSE, error_message="empty"
        )

    assert session.rolled_back is True


def test_record_failure_evidence_rejects_missing_quota_reservation() -> None:
    """Scan with no quota reservation -> ConflictError."""
    session = _MockSession()
    run = _make_run()
    scan = _make_scan(quota_reservation_id=None)
    evidence = _make_evidence()

    recorder, _, _, _, _, _ = _setup_recorder_mocks(session, run=run, scan=scan)

    with pytest.raises(ConflictError):
        recorder.record_failure_evidence(
            run.id, evidence, error_code=ProviderErrorCode.MALFORMED_RESPONSE, error_message="empty"
        )

    assert session.rolled_back is True


def test_record_failure_evidence_rolls_back_on_quota_failure() -> None:
    """If quota.commit_ai_checks raises, session is rolled back."""
    session = _MockSession()
    run = _make_run()
    scan = _make_scan()
    evidence = _make_evidence()

    recorder, _, _, _, _, quota = _setup_recorder_mocks(session, run=run, scan=scan)
    quota.commit_ai_checks.side_effect = ConflictError("quota conflict")

    with pytest.raises(ConflictError):
        recorder.record_failure_evidence(
            run.id, evidence, error_code=ProviderErrorCode.MALFORMED_RESPONSE, error_message="empty"
        )

    assert session.rolled_back is True
    assert session.committed is False


def test_record_failure_evidence_with_max_tool_calls_violation() -> None:
    """Evidence with max_tool_calls_violation=2 -> preserved, not clamped."""
    session = _MockSession()
    run = _make_run()
    scan = _make_scan()
    evidence = _make_evidence(
        usage=ProviderUsage(
            input_tokens=40,
            output_tokens=0,
            total_tokens=40,
            search_requests=2,
        ),
        max_tool_calls_violation=2,
    )

    recorder, _, _, _, _, quota = _setup_recorder_mocks(session, run=run, scan=scan)

    recorder.record_failure_evidence(
        run.id, evidence, error_code=ProviderErrorCode.MALFORMED_RESPONSE, error_message="empty"
    )

    assert session.committed is True
    assert run.search_requests == 2  # NOT clamped
    # Quota committed with the raw count
    call_kwargs = quota.commit_ai_checks.call_args.kwargs
    assert call_kwargs["search_requests"] == 2


def test_record_failure_evidence_with_incomplete_reason() -> None:
    """Evidence with incomplete_reason='max_output_tokens' -> error_message
    can include it, and the run is FAILED."""
    session = _MockSession()
    run = _make_run()
    scan = _make_scan()
    evidence = _make_evidence(incomplete_reason="max_output_tokens")

    recorder, _, _, _, _, _ = _setup_recorder_mocks(session, run=run, scan=scan)

    recorder.record_failure_evidence(
        run.id,
        evidence,
        error_code=ProviderErrorCode.MALFORMED_RESPONSE,
        error_message="OpenAI returned an empty response text (incomplete: max_output_tokens).",
    )

    assert session.committed is True
    assert run.status == PromptRunStatus.FAILED
    assert run.error_message is not None
    assert "max_output_tokens" in run.error_message


def test_record_failure_evidence_truncates_long_error_message() -> None:
    """error_message is truncated to 1000 chars."""
    session = _MockSession()
    run = _make_run()
    scan = _make_scan()
    evidence = _make_evidence()

    recorder, _, _, _, _, _ = _setup_recorder_mocks(session, run=run, scan=scan)

    long_msg = "x" * 2000
    recorder.record_failure_evidence(
        run.id, evidence, error_code=ProviderErrorCode.MALFORMED_RESPONSE, error_message=long_msg
    )

    assert len(run.error_message) == 1000  # type: ignore[arg-type]


def test_record_failure_evidence_persists_citations() -> None:
    """Citations from failure evidence are persisted as ResponseSource rows."""
    session = _MockSession()
    run = _make_run()
    scan = _make_scan()
    evidence = _make_evidence(
        citations=(
            ProviderCitation(url="https://example.com/1", title="First"),
            ProviderCitation(url="https://example.com/2", title="Second"),
        )
    )

    recorder, _, _, sources_repo, _, _ = _setup_recorder_mocks(session, run=run, scan=scan)

    recorder.record_failure_evidence(
        run.id, evidence, error_code=ProviderErrorCode.MALFORMED_RESPONSE, error_message="empty"
    )

    sources_repo.create_batch_idempotent.assert_called_once()
    batch = sources_repo.create_batch_idempotent.call_args.args[0]
    assert len(batch) == 2


def test_record_failure_evidence_unknown_cost_still_persists() -> None:
    """When cost calculation is incomplete (no pricing rule), cost_usd is None
    but usage/IDs are still persisted and quota is still committed."""
    session = _MockSession()
    run = _make_run()
    scan = _make_scan()
    evidence = _make_evidence()

    recorder, _, _, _, calculator, quota = _setup_recorder_mocks(
        session,
        run=run,
        scan=scan,
        cost=CostComputation(
            cost_usd=None,
            calculated_cost_usd=None,
            provider_reported_cost_usd=None,
            source=CostSource.UNKNOWN,
            complete=False,
            pricing_rule_id=None,
        ),
    )

    recorder.record_failure_evidence(
        run.id, evidence, error_code=ProviderErrorCode.MALFORMED_RESPONSE, error_message="empty"
    )

    assert session.committed is True
    assert run.cost_usd is None
    assert run.cost_source == CostSource.UNKNOWN
    # Quota still committed — the provider call was billable
    quota.commit_ai_checks.assert_called_once()
    assert run.usage_event_id is not None


# ---------------------------------------------------------------------------
# Phase 13.5.10B — Contract violation and historical auditability
# ---------------------------------------------------------------------------


def test_record_contract_violation_evidence() -> None:
    """ProviderContractViolationError evidence → FAILED with
    PROVIDER_CONTRACT_VIOLATION, search_requests=2, cost uses 2,
    UsageEvent created, AI Check committed=1."""
    session = _MockSession()
    run = _make_run()
    scan = _make_scan()
    evidence = _make_evidence(
        usage=ProviderUsage(
            input_tokens=40,
            output_tokens=10,
            total_tokens=50,
            search_requests=2,
        ),
        max_tool_calls_violation=2,
        requested_max_tool_calls=1,
        observed_search_requests=2,
    )

    recorder, _, _, _, _, quota = _setup_recorder_mocks(session, run=run, scan=scan)

    recorder.record_failure_evidence(
        run.id,
        evidence,
        error_code=ProviderErrorCode.PROVIDER_CONTRACT_VIOLATION,
        error_message="OpenAI exceeded requested max_tool_calls: requested=1 observed=2.",
    )

    assert session.committed is True
    assert run.status == PromptRunStatus.FAILED
    assert run.error_code == ProviderErrorCode.PROVIDER_CONTRACT_VIOLATION
    assert "requested=1" in run.error_message  # type: ignore[operator]
    assert "observed=2" in run.error_message  # type: ignore[operator]
    # search_requests preserved, NOT clamped
    assert run.search_requests == 2
    # Quota committed with raw count
    call_kwargs = quota.commit_ai_checks.call_args.kwargs
    assert call_kwargs["search_requests"] == 2
    assert call_kwargs["quantity"] == 1
    # UsageEvent created
    assert run.usage_event_id is not None


def test_contract_violation_historical_auditability() -> None:
    """After persistence, the PromptRun alone must prove requested=1
    and observed=2, WITHOUT querying current settings.

    The error_message contains the deterministic string
    'requested=1 observed=2', and search_requests=2 is persisted.
    This is sufficient for historical auditability without a migration
    to add a requested_max_tool_calls column."""
    session = _MockSession()
    run = _make_run()
    scan = _make_scan()
    evidence = _make_evidence(
        usage=ProviderUsage(
            input_tokens=40,
            output_tokens=10,
            total_tokens=50,
            search_requests=2,
        ),
        max_tool_calls_violation=2,
        requested_max_tool_calls=1,
        observed_search_requests=2,
    )

    recorder, _, _, _, _, _ = _setup_recorder_mocks(session, run=run, scan=scan)

    recorder.record_failure_evidence(
        run.id,
        evidence,
        error_code=ProviderErrorCode.PROVIDER_CONTRACT_VIOLATION,
        error_message="OpenAI exceeded requested max_tool_calls: requested=1 observed=2.",
    )

    # Historical auditability: from persisted PromptRun fields alone
    assert run.error_code == ProviderErrorCode.PROVIDER_CONTRACT_VIOLATION
    assert run.error_message is not None
    assert "requested=1" in run.error_message
    assert "observed=2" in run.error_message
    assert run.search_requests == 2
    # No need to query settings — the error_message is the durable proof


# ---------------------------------------------------------------------------
# Phase 13.5.10D — Multi-failure durability and enrich_error_message
# ---------------------------------------------------------------------------


def test_enrich_error_message_appends_incomplete_reason() -> None:
    """enrich_error_message appends incomplete_reason to primary message."""
    from app.services.scanning.errors import enrich_error_message

    evidence = _make_evidence(incomplete_reason="max_output_tokens")
    result = enrich_error_message("OpenAI returned an empty response text.", evidence)
    assert "OpenAI returned an empty response text." in result
    assert "incomplete_reason=max_output_tokens" in result


def test_enrich_error_message_appends_max_tool_calls() -> None:
    """enrich_error_message appends requested/observed max_tool_calls."""
    from app.services.scanning.errors import enrich_error_message

    evidence = _make_evidence(
        incomplete_reason=None,
        max_tool_calls_violation=2,
        requested_max_tool_calls=1,
        observed_search_requests=2,
    )
    result = enrich_error_message("OpenAI returned an empty response text.", evidence)
    assert "requested_max_tool_calls=1" in result
    assert "observed_search_requests=2" in result


def test_enrich_error_message_no_evidence_returns_primary() -> None:
    """enrich_error_message with None evidence returns primary unchanged."""
    from app.services.scanning.errors import enrich_error_message

    result = enrich_error_message("Primary error.", None)
    assert result == "Primary error."


def test_enrich_error_message_truncates_to_1000() -> None:
    """enrich_error_message truncates to 1000 chars."""
    from app.services.scanning.errors import enrich_error_message

    long_msg = "x" * 950
    evidence = _make_evidence(
        incomplete_reason="max_output_tokens",
        requested_max_tool_calls=1,
        observed_search_requests=2,
    )
    result = enrich_error_message(long_msg, evidence)
    assert len(result) <= 1000


def test_enrich_error_message_no_secondary_returns_primary() -> None:
    """enrich_error_message with no secondary fields returns primary."""
    from app.services.scanning.errors import enrich_error_message

    evidence = _make_evidence(incomplete_reason=None)
    result = enrich_error_message("Primary only.", evidence)
    assert result == "Primary only."


# ---------------------------------------------------------------------------
# Phase 13.5.10H — Secondary evidence truncation matrix
# ---------------------------------------------------------------------------


def test_enrich_error_message_truncation_matrix() -> None:
    """For all primary lengths (100, 900, 990, 1000, 1100), secondary
    evidence must be preserved and result <= 1000 chars."""
    from app.services.scanning.errors import enrich_error_message

    evidence = _make_evidence(
        incomplete_reason="max_output_tokens",
        requested_max_tool_calls=1,
        observed_search_requests=2,
    )

    required_keys = [
        "incomplete_reason=max_output_tokens",
        "requested_max_tool_calls=1",
        "observed_search_requests=2",
    ]

    for n in [100, 900, 990, 1000, 1100]:
        primary = "x" * n
        result = enrich_error_message(primary, evidence)
        assert len(result) <= 1000, f"len={len(result)} for primary={n}"
        for key in required_keys:
            assert key in result, f"Missing '{key}' for primary={n}: {result[-100:]}"


def test_enrich_error_message_long_primary_no_secondary() -> None:
    """Primary >1000 with no secondary evidence → simply truncated."""
    from app.services.scanning.errors import enrich_error_message

    result = enrich_error_message("x" * 1100, None)
    assert len(result) <= 1000


def test_enrich_error_message_incomplete_reason_sanitized() -> None:
    """incomplete_reason is truncated to 200 chars."""
    from app.services.scanning.errors import enrich_error_message

    long_reason = "y" * 500
    evidence = _make_evidence(
        incomplete_reason=long_reason,
        requested_max_tool_calls=1,
        observed_search_requests=2,
    )
    result = enrich_error_message("Short primary.", evidence)
    assert len(result) <= 1000
    # incomplete_reason is truncated to 200 chars in the suffix
    assert "incomplete_reason=" in result
    assert "requested_max_tool_calls=1" in result
    assert "observed_search_requests=2" in result
    # The 500-char reason should NOT appear in full
    assert "y" * 500 not in result


# ---------------------------------------------------------------------------
# Phase 13.5.10H — Success reconciliation cleanup
# ---------------------------------------------------------------------------


def test_success_reconciliation_clears_incident_markers() -> None:
    """RUNNING + ACCOUNTING_UNRESOLVED → record(valid ProviderResult)
    → SUCCEEDED + error_code=None + error_message=None."""
    from app.providers.base import ProviderResult

    run = _make_run(status=PromptRunStatus.RUNNING)
    run.error_code = ProviderErrorCode.ACCOUNTING_UNRESOLVED
    run.error_message = "Accounting unresolved after provider response."

    scan = _make_scan()
    session = _MockSession()
    recorder, _, _, sources_repo, _, _ = _setup_recorder_mocks(session, run=run, scan=scan)

    result = ProviderResult(
        provider=LLMProvider.OPENAI,
        surface=ProviderSurface.OPENAI_RESPONSES_API,
        execution_mode=ProviderExecutionMode.WEB_GROUNDED,
        requested_model="gpt-5.6-terra",
        returned_model="gpt-5.6-terra",
        response_text="Valid response.",
        citations=(),
        usage=ProviderUsage(input_tokens=100, output_tokens=50, total_tokens=150),
        provider_request_id="req_001",
        provider_response_id="resp_001",
        finish_reason="stop",
        latency_ms=100,
        search_used=True,
    )

    recorder.record(run.id, result)

    assert run.status == PromptRunStatus.SUCCEEDED
    assert run.error_code is None
    assert run.error_message is None
    assert run.usage_event_id is not None
    assert run.completed_at is not None


# ---------------------------------------------------------------------------
# Phase 13.5.10H — Failure reconciliation cleanup
# ---------------------------------------------------------------------------


def test_failure_reconciliation_replaces_incident_markers() -> None:
    """RUNNING + ACCOUNTING_UNRESOLVED → record_failure_evidence(...)
    → FAILED + functional error_code + ACCOUNTING_UNRESOLVED replaced."""
    run = _make_run(status=PromptRunStatus.RUNNING)
    run.error_code = ProviderErrorCode.ACCOUNTING_UNRESOLVED
    run.error_message = "Accounting unresolved after provider response."

    scan = _make_scan()
    session = _MockSession()
    recorder, _, _, _, _, _ = _setup_recorder_mocks(session, run=run, scan=scan)

    evidence = _make_evidence(
        incomplete_reason="max_output_tokens",
        max_tool_calls_violation=2,
        requested_max_tool_calls=1,
        observed_search_requests=2,
    )

    recorder.record_failure_evidence(
        run.id,
        evidence,
        error_code=ProviderErrorCode.PROVIDER_CONTRACT_VIOLATION,
        error_message="OpenAI exceeded requested max_tool_calls: requested=1 observed=2.",
    )

    assert run.status == PromptRunStatus.FAILED
    assert run.error_code == ProviderErrorCode.PROVIDER_CONTRACT_VIOLATION
    assert "requested=1" in (run.error_message or "")
    assert run.usage_event_id is not None


# ---------------------------------------------------------------------------
# Phase 13.5.10H — Citation reconciliation idempotency
# ---------------------------------------------------------------------------


def test_citation_reconciliation_same_citations_no_duplicate() -> None:
    """Incident state has citations. record() with same citations
    → no IntegrityError, no duplicate."""
    from app.providers.base import ProviderResult

    run = _make_run(status=PromptRunStatus.RUNNING)
    run.error_code = ProviderErrorCode.ACCOUNTING_UNRESOLVED

    scan = _make_scan()
    session = _MockSession()
    recorder, _, _, sources_repo, _, _ = _setup_recorder_mocks(session, run=run, scan=scan)

    # sources_repo is a mock — create_batch_idempotent is a no-op mock
    result = ProviderResult(
        provider=LLMProvider.OPENAI,
        surface=ProviderSurface.OPENAI_RESPONSES_API,
        execution_mode=ProviderExecutionMode.WEB_GROUNDED,
        requested_model="gpt-5.6-terra",
        returned_model="gpt-5.6-terra",
        response_text="Valid response.",
        citations=(
            ProviderCitation(
                url="https://example.com/a",
                title="Example A",
                source_type="web",
            ),
        ),
        usage=ProviderUsage(input_tokens=100, output_tokens=50, total_tokens=150),
        provider_request_id="req_001",
        provider_response_id="resp_001",
        finish_reason="stop",
        latency_ms=100,
        search_used=True,
    )

    # Should NOT raise — idempotent (mock handles it)
    recorder.record(run.id, result)

    assert run.status == PromptRunStatus.SUCCEEDED
    sources_repo.create_batch_idempotent.assert_called_once()


def test_citation_reconciliation_conflict_raises() -> None:
    """Existing ordinal=1 URL=A, incoming ordinal=1 URL=C
    → ConflictError, rollback, no terminalization."""
    from app.core.exceptions import ConflictError as CoreConflictError
    from app.repositories.scan_repository import ResponseSourceRepository

    run_id = uuid.uuid4()

    session = MagicMock()
    existing_source = MagicMock(spec=ResponseSource)
    existing_source.prompt_run_id = run_id
    existing_source.ordinal = 1
    existing_source.url = "https://example.com/a"

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [existing_source]
    session.execute = MagicMock(return_value=mock_result)

    sources_repo = ResponseSourceRepository(session)

    incoming = ResponseSource(
        prompt_run_id=run_id,
        ordinal=1,
        url="https://example.com/c",  # Different URL!
        title="Example C",
        source_type="web",
    )

    with pytest.raises(CoreConflictError):
        sources_repo.create_batch_idempotent([incoming])


def test_citation_reconciliation_same_url_noop() -> None:
    """Existing ordinal=1 URL=A, incoming ordinal=1 URL=A
    → no-op, no error, no duplicate."""
    from app.repositories.scan_repository import ResponseSourceRepository

    run_id = uuid.uuid4()

    session = MagicMock()
    existing_source = MagicMock(spec=ResponseSource)
    existing_source.prompt_run_id = run_id
    existing_source.ordinal = 1
    existing_source.url = "https://example.com/a"

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [existing_source]
    session.execute = MagicMock(return_value=mock_result)

    sources_repo = ResponseSourceRepository(session)

    incoming = ResponseSource(
        prompt_run_id=run_id,
        ordinal=1,
        url="https://example.com/a",  # Same URL
        title="Example A",
        source_type="web",
    )

    # Should NOT raise
    result = sources_repo.create_batch_idempotent([incoming])
    # No new sources created
    assert result == []


# ---------------------------------------------------------------------------
# Phase 13.5.10H — Incident citation persistence
# ---------------------------------------------------------------------------


def test_incident_citation_persistence_idempotent() -> None:
    """Incident persistence with citations → second call with same
    citations → no duplicate, no error."""
    from app.repositories.scan_repository import ResponseSourceRepository

    run_id = uuid.uuid4()

    session = MagicMock()
    existing_source = MagicMock(spec=ResponseSource)
    existing_source.prompt_run_id = run_id
    existing_source.ordinal = 1
    existing_source.url = "https://example.com/a"

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [existing_source]
    session.execute = MagicMock(return_value=mock_result)

    sources_repo = ResponseSourceRepository(session)

    # Same citation as existing → no-op
    incoming = ResponseSource(
        prompt_run_id=run_id,
        ordinal=1,
        url="https://example.com/a",
        title="Example A",
        source_type="web",
    )

    result = sources_repo.create_batch_idempotent([incoming])
    assert result == []  # No new sources
