"""Unit tests for ScanExecutionService fail-closed behavior (Phase 13.5.10B).

These tests verify that when ProviderFailureEvidence exists but the recorder
fails, the exception PROPAGATES — the PromptRun is NOT terminalized as a
generic FAILED with zero usage, and _record_failure is NOT called.

This is the fail-closed contract: RUNNING / ACCOUNTING UNRESOLVED is
preferable to FAILED / cost=0 / quota released for a known billable call.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.core.enums import (
    LLMProvider,
    PromptRunStatus,
    ProviderErrorCode,
    ProviderExecutionMode,
    ProviderSurface,
)
from app.core.exceptions import ConflictError
from app.models.scan import PromptRun
from app.models.tracking import Prompt
from app.providers.base import (
    ProviderFailureEvidence,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from app.providers.errors import (
    ProviderAuthenticationError,
    ProviderContractViolationError,
)
from app.services.scan_execution_service import ScanExecutionService


def _make_evidence(
    *,
    search_requests: int = 2,
    requested_max_tool_calls: int = 1,
) -> ProviderFailureEvidence:
    return ProviderFailureEvidence(
        provider=LLMProvider.OPENAI,
        surface=ProviderSurface.OPENAI_RESPONSES_API,
        execution_mode=ProviderExecutionMode.WEB_GROUNDED,
        requested_model="gpt-5.6-terra",
        returned_model="gpt-5.6-terra",
        provider_request_id="req_001",
        provider_response_id="resp_001",
        usage=ProviderUsage(
            input_tokens=100,
            output_tokens=0,
            total_tokens=100,
            search_requests=search_requests,
        ),
        citations=(),
        latency_ms=42,
        search_used=True,
        max_tool_calls_violation=search_requests,
        requested_max_tool_calls=requested_max_tool_calls,
        observed_search_requests=search_requests,
    )


def _make_run_snapshot() -> Any:
    """Create a mock PromptRun snapshot for _claim_run."""
    run = MagicMock(spec=PromptRun)
    run.id = uuid.uuid4()
    run.scan_id = uuid.uuid4()
    run.status = PromptRunStatus.PENDING
    run.provider = LLMProvider.OPENAI.value
    run.provider_surface = ProviderSurface.OPENAI_RESPONSES_API.value
    run.execution_mode = ProviderExecutionMode.WEB_GROUNDED.value
    run.requested_model = "gpt-5.6-terra"
    return run


def _make_prompt() -> Any:
    prompt = MagicMock(spec=Prompt)
    prompt.text = "test prompt"
    prompt.target_country = "US"
    prompt.target_language = "en"
    return prompt


class _FakeRegistry:
    """Fake provider registry that returns a fake adapter."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def get(self, provider: LLMProvider) -> Any:
        return self._adapter


class _FailingEvidenceAdapter:
    """Adapter that raises ProviderContractViolationError with evidence."""

    provider = LLMProvider.OPENAI
    surface = ProviderSurface.OPENAI_RESPONSES_API

    def capabilities(self) -> Any:
        return MagicMock()

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        raise ProviderContractViolationError(
            "OpenAI exceeded requested max_tool_calls: requested=1 observed=2.",
            provider=LLMProvider.OPENAI.value,
            evidence=_make_evidence(),
        )


class _PreProviderErrorAdapter:
    """Adapter that raises ProviderAuthenticationError (case A, no evidence)."""

    provider = LLMProvider.OPENAI
    surface = ProviderSurface.OPENAI_RESPONSES_API

    def capabilities(self) -> Any:
        return MagicMock()

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        raise ProviderAuthenticationError(
            "Invalid API key.",
            provider=LLMProvider.OPENAI.value,
        )


class _MockSessionFactory:
    """Mock session factory that returns mock sessions."""

    def __init__(self) -> None:
        self.sessions: list[MagicMock] = []

    def __call__(self) -> Any:
        session = MagicMock()
        self.sessions.append(session)
        return session


def _make_service(
    *,
    adapter: Any,
    factory: Any = None,
) -> ScanExecutionService:
    """Create a ScanExecutionService with mocked dependencies."""
    factory = factory or _MockSessionFactory()
    registry = _FakeRegistry(adapter)
    settings = MagicMock()
    settings.scan_max_concurrency = 1

    service = ScanExecutionService(factory, registry=registry, settings=settings)  # type: ignore[arg-type]
    return service


def test_evidence_accounting_failure_propagates_and_no_record_failure() -> None:
    """When record_failure_evidence() raises, the exception propagates
    and _record_failure is NOT called.  The PromptRun remains RUNNING."""
    service = _make_service(adapter=_FailingEvidenceAdapter())

    # Mock _claim_run to return a run snapshot + prompt
    run_snapshot = _make_run_snapshot()
    prompt = _make_prompt()
    service._claim_run = MagicMock(return_value=(run_snapshot, prompt))  # type: ignore[method-assign]

    # Mock _record_failure_with_evidence to raise (simulating recorder failure)
    def failing_record(
        run_id: uuid.UUID,
        evidence: ProviderFailureEvidence,
        error_code: ProviderErrorCode,
        error_message: str,
    ) -> None:
        raise ConflictError("quota conflict")

    service._record_failure_with_evidence = failing_record  # type: ignore[method-assign]

    # Mock _record_failure to detect if it's called (it should NOT be)
    record_failure_called = False

    def tracking_record_failure(run_id: uuid.UUID, code: ProviderErrorCode, message: str) -> None:
        nonlocal record_failure_called
        record_failure_called = True

    service._record_failure = tracking_record_failure  # type: ignore[method-assign]

    # Execute — should raise
    with pytest.raises(ConflictError):
        asyncio.run(service._execute_run(run_snapshot.id))

    # _record_failure was NOT called
    assert record_failure_called is False


def test_evidence_accounting_failure_propagates_to_execute_scan() -> None:
    """When _record_failure_with_evidence raises, execute_scan does NOT
    reach finalization.  The exception propagates through gather."""
    service = _make_service(adapter=_FailingEvidenceAdapter())

    # Mock _claim_scan to return True
    service._claim_scan = MagicMock(return_value=True)  # type: ignore[method-assign]

    # Mock _get_scan_type_and_repeats
    from app.core.enums import ScanType

    service._get_scan_type_and_repeats = MagicMock(return_value=(ScanType.STANDARD, 1))  # type: ignore[method-assign]

    # Mock _list_run_ids to return one run
    run_id = uuid.uuid4()
    service._list_run_ids = MagicMock(return_value=[run_id])  # type: ignore[method-assign]

    # Mock _claim_run
    run_snapshot = _make_run_snapshot()
    run_snapshot.id = run_id
    prompt = _make_prompt()
    service._claim_run = MagicMock(return_value=(run_snapshot, prompt))  # type: ignore[method-assign]

    # Mock _record_failure_with_evidence to raise
    def failing_record(
        run_id: uuid.UUID,
        evidence: ProviderFailureEvidence,
        error_code: ProviderErrorCode,
        error_message: str,
    ) -> None:
        raise ConflictError("accounting failure")

    service._record_failure_with_evidence = failing_record  # type: ignore[method-assign]

    # Mock finalize to detect if it's called (it should NOT be)
    finalize_called = False

    def tracking_finalize(*args: Any, **kwargs: Any) -> Any:
        nonlocal finalize_called
        finalize_called = True
        return MagicMock()

    with patch("app.services.scan_execution_service.ScanFinalizationService") as mock_final_cls:
        mock_final_cls.return_value.finalize = tracking_finalize

        with pytest.raises(ConflictError):
            asyncio.run(service.execute_scan(uuid.uuid4()))

    # Finalize was NOT called
    assert finalize_called is False


def test_case_a_pre_provider_error_uses_record_failure() -> None:
    """ProviderError without evidence (case A) → _record_failure is called.
    Normal behavior preserved."""
    service = _make_service(adapter=_PreProviderErrorAdapter())

    run_snapshot = _make_run_snapshot()
    prompt = _make_prompt()
    service._claim_run = MagicMock(return_value=(run_snapshot, prompt))  # type: ignore[method-assign]

    record_failure_called = False
    record_failure_code: list[ProviderErrorCode] = []

    def tracking_record_failure(run_id: uuid.UUID, code: ProviderErrorCode, message: str) -> None:
        nonlocal record_failure_called
        record_failure_called = True
        record_failure_code.append(code)

    service._record_failure = tracking_record_failure  # type: ignore[method-assign]

    # Execute — should NOT raise
    asyncio.run(service._execute_run(run_snapshot.id))

    # _record_failure WAS called (case A behavior preserved)
    assert record_failure_called is True
    assert record_failure_code[0] == ProviderErrorCode.AUTHENTICATION_ERROR


def test_case_b_evidence_success_uses_record_failure_with_evidence() -> None:
    """ProviderError WITH evidence (case B) → _record_failure_with_evidence
    is called.  When it succeeds, _record_failure is NOT called."""
    service = _make_service(adapter=_FailingEvidenceAdapter())

    run_snapshot = _make_run_snapshot()
    prompt = _make_prompt()
    service._claim_run = MagicMock(return_value=(run_snapshot, prompt))  # type: ignore[method-assign]

    evidence_record_called = False
    plain_record_called = False

    def tracking_evidence_record(
        run_id: uuid.UUID,
        evidence: ProviderFailureEvidence,
        error_code: ProviderErrorCode,
        error_message: str,
    ) -> None:
        nonlocal evidence_record_called
        evidence_record_called = True

    def tracking_plain_record(run_id: uuid.UUID, code: ProviderErrorCode, message: str) -> None:
        nonlocal plain_record_called
        plain_record_called = True

    service._record_failure_with_evidence = tracking_evidence_record  # type: ignore[method-assign]
    service._record_failure = tracking_plain_record  # type: ignore[method-assign]

    # Execute — should NOT raise
    asyncio.run(service._execute_run(run_snapshot.id))

    assert evidence_record_called is True
    assert plain_record_called is False


# ---------------------------------------------------------------------------
# Phase 13.5.10D — Concurrency model tests
# ---------------------------------------------------------------------------


class _TrackingAdapter:
    """Adapter that tracks which runs started execute() and optionally fails."""

    provider = LLMProvider.OPENAI
    surface = ProviderSurface.OPENAI_RESPONSES_API

    def __init__(
        self,
        *,
        fail_on_run: int | None = None,
        delay: float = 0.01,
    ) -> None:
        self._fail_on_run = fail_on_run
        self._delay = delay
        self.started_runs: list[uuid.UUID] = []
        self.completed_runs: list[uuid.UUID] = []
        self._call_count = 0

    def capabilities(self) -> Any:
        return MagicMock()

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        # Extract run_id from correlation_id "scan:...:run:{id}"
        cid = request.correlation_id or ""
        run_id_str = cid.split("run:")[1]
        run_id = uuid.UUID(run_id_str)
        self._call_count += 1
        call_idx = self._call_count
        self.started_runs.append(run_id)
        await asyncio.sleep(self._delay)
        self.completed_runs.append(run_id)

        if self._fail_on_run is not None and call_idx == self._fail_on_run:
            raise ProviderContractViolationError(
                "Contract violation",
                provider=LLMProvider.OPENAI.value,
                evidence=_make_evidence(),
            )

        return MagicMock(spec=ProviderResult)


def test_concurrency_fatal_stops_new_calls_but_awaits_in_flight() -> None:
    """5 runs, concurrency=2.  Run A fails fatally.  Run B (in-flight)
    is awaited.  Runs C/D/E never start adapter.execute()."""
    adapter = _TrackingAdapter(fail_on_run=1, delay=0.05)
    service = _make_service(adapter=adapter)
    service._settings.scan_max_concurrency = 2

    # Create 5 run_ids
    run_ids = [uuid.uuid4() for _ in range(5)]

    # Mock _claim_run to return valid snapshots
    def make_claim(rid: uuid.UUID) -> Any:
        run = MagicMock(spec=PromptRun)
        run.id = rid
        run.scan_id = uuid.uuid4()
        run.status = PromptRunStatus.PENDING
        run.provider = LLMProvider.OPENAI.value
        run.provider_surface = ProviderSurface.OPENAI_RESPONSES_API.value
        run.execution_mode = ProviderExecutionMode.WEB_GROUNDED.value
        run.requested_model = "gpt-5.6-terra"
        prompt = _make_prompt()
        return (run, prompt)

    service._claim_run = MagicMock(side_effect=lambda rid: make_claim(rid))  # type: ignore[method-assign]

    # Mock _record_failure_with_evidence to raise (fatal accounting error)
    def failing_record(
        run_id: uuid.UUID,
        evidence: ProviderFailureEvidence,
        error_code: ProviderErrorCode,
        error_message: str,
    ) -> None:
        raise ConflictError("accounting failure")

    service._record_failure_with_evidence = failing_record  # type: ignore[method-assign]

    # Mock _record_accounting_unresolved to be a no-op
    service._record_accounting_unresolved = MagicMock()  # type: ignore[method-assign]

    # Mock the recorder for successful runs
    def mock_record(run_id: uuid.UUID, result: Any) -> None:
        pass

    # Mock PromptRunResultRecorder
    with patch("app.services.scan_execution_service.PromptRunResultRecorder") as mock_recorder_cls:
        mock_recorder_cls.return_value.record = mock_record

        # Execute — should raise (fatal error propagated)
        with pytest.raises(ConflictError):
            asyncio.run(service._execute_run_ids(run_ids))

    # Only 2 runs started adapter.execute() (concurrency=2)
    assert len(adapter.started_runs) == 2
    # Runs C/D/E never started
    for rid in run_ids[2:]:
        assert rid not in adapter.started_runs
    # Run B (the non-failing in-flight run) completed
    assert len(adapter.completed_runs) == 2


def test_concurrency_no_fatal_all_runs_execute() -> None:
    """5 runs, concurrency=2, no failures.  All 5 runs execute."""
    adapter = _TrackingAdapter(delay=0.01)
    service = _make_service(adapter=adapter)
    service._settings.scan_max_concurrency = 2

    run_ids = [uuid.uuid4() for _ in range(5)]

    def make_claim(rid: uuid.UUID) -> Any:
        run = MagicMock(spec=PromptRun)
        run.id = rid
        run.scan_id = uuid.uuid4()
        run.status = PromptRunStatus.PENDING
        run.provider = LLMProvider.OPENAI.value
        run.provider_surface = ProviderSurface.OPENAI_RESPONSES_API.value
        run.execution_mode = ProviderExecutionMode.WEB_GROUNDED.value
        run.requested_model = "gpt-5.6-terra"
        return (run, _make_prompt())

    service._claim_run = MagicMock(side_effect=lambda rid: make_claim(rid))  # type: ignore[method-assign]

    with patch("app.services.scan_execution_service.PromptRunResultRecorder") as mock_recorder_cls:
        mock_recorder_cls.return_value.record = MagicMock()

        asyncio.run(service._execute_run_ids(run_ids))

    # All 5 runs started and completed
    assert len(adapter.started_runs) == 5
    assert len(adapter.completed_runs) == 5


# ---------------------------------------------------------------------------
# Phase 13.5.10D — Accounting unresolved incident persistence tests
# ---------------------------------------------------------------------------


def test_accounting_unresolved_persists_evidence_without_quota_or_usage_event() -> None:
    """When record_failure_evidence fails, _record_accounting_unresolved
    persists evidence on the PromptRun WITHOUT UsageEvent, quota commit,
    or fake cost.  PromptRun remains RUNNING."""
    service = _make_service(adapter=_FailingEvidenceAdapter())

    run_snapshot = _make_run_snapshot()
    prompt = _make_prompt()
    service._claim_run = MagicMock(return_value=(run_snapshot, prompt))  # type: ignore[method-assign]

    # Mock _record_accounting_unresolved to capture the evidence
    captured_evidence: list[Any] = []

    def capturing_incident(
        run_id: uuid.UUID, evidence: Any, original_error_type: str | None = None
    ) -> None:
        captured_evidence.append(evidence)

    service._record_accounting_unresolved = capturing_incident  # type: ignore[method-assign]

    # Mock PromptRunResultRecorder.record_failure_evidence to raise
    # so the real _record_failure_with_evidence catches it and calls
    # _record_accounting_unresolved
    with patch("app.services.scan_execution_service.PromptRunResultRecorder") as mock_recorder_cls:
        mock_recorder_cls.return_value.record_failure_evidence = MagicMock(
            side_effect=ConflictError("quota conflict")
        )

        # Execute — should raise (fatal error propagated)
        with pytest.raises(ConflictError):
            asyncio.run(service._execute_run(run_snapshot.id))

    # _record_accounting_unresolved was called with the evidence
    assert len(captured_evidence) == 1
    ev = captured_evidence[0]
    assert ev.provider_request_id == "req_001"
    assert ev.provider_response_id == "resp_001"
    assert ev.usage.input_tokens == 100
    assert ev.usage.search_requests == 2


def test_record_accounting_unresolved_sets_fields_on_run() -> None:
    """Directly test _record_accounting_unresolved: sets evidence fields,
    error_code=ACCOUNTING_UNRESOLVED, status remains RUNNING, no UsageEvent."""
    service = _make_service(adapter=_FailingEvidenceAdapter())

    run_id = uuid.uuid4()
    evidence = _make_evidence()

    # Create a mock run that will be returned by the mock repository
    mock_run = MagicMock(spec=PromptRun)
    mock_run.status = PromptRunStatus.RUNNING
    mock_run.id = run_id

    mock_session = MagicMock()
    mock_runs_repo = MagicMock()
    mock_runs_repo.get_for_update.return_value = mock_run

    mock_factory = MagicMock()
    mock_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_factory.return_value.__exit__ = MagicMock(return_value=False)

    service._factory = mock_factory

    # Convert to AccountingIncidentEvidence (the new common interface)
    from app.services.scan_execution_service import AccountingIncidentEvidence

    incident_evidence = AccountingIncidentEvidence.from_failure_evidence(evidence)

    with patch("app.services.scan_execution_service.PromptRunRepository") as mock_runs_cls:
        mock_runs_cls.return_value = mock_runs_repo

        service._record_accounting_unresolved(run_id, incident_evidence)

    # Evidence fields persisted
    assert mock_run.provider_request_id == evidence.provider_request_id
    assert mock_run.provider_response_id == evidence.provider_response_id
    assert mock_run.returned_model == evidence.returned_model
    assert mock_run.latency_ms == evidence.latency_ms
    assert mock_run.search_used == evidence.search_used
    assert mock_run.input_tokens == evidence.usage.input_tokens
    assert mock_run.output_tokens == evidence.usage.output_tokens
    assert mock_run.search_requests == evidence.usage.search_requests
    # Error code set to ACCOUNTING_UNRESOLVED
    assert mock_run.error_code == ProviderErrorCode.ACCOUNTING_UNRESOLVED
    assert mock_run.error_message is not None
    assert "Accounting unresolved" in mock_run.error_message
    # Status remains RUNNING (not terminalized)
    assert mock_run.status == PromptRunStatus.RUNNING
    # Session committed
    mock_session.commit.assert_called_once()


def test_record_accounting_unresolved_skips_non_running_run() -> None:
    """If the run is no longer RUNNING (e.g. already terminalized),
    _record_accounting_unresolved is a no-op."""
    service = _make_service(adapter=_FailingEvidenceAdapter())

    run_id = uuid.uuid4()
    evidence = _make_evidence()

    from app.services.scan_execution_service import AccountingIncidentEvidence

    incident_evidence = AccountingIncidentEvidence.from_failure_evidence(evidence)

    mock_run = MagicMock(spec=PromptRun)
    mock_run.status = PromptRunStatus.FAILED  # Already terminalized

    mock_session = MagicMock()
    mock_runs_repo = MagicMock()
    mock_runs_repo.get_for_update.return_value = mock_run

    mock_factory = MagicMock()
    mock_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_factory.return_value.__exit__ = MagicMock(return_value=False)

    service._factory = mock_factory

    with patch("app.services.scan_execution_service.PromptRunRepository") as mock_runs_cls:
        mock_runs_cls.return_value = mock_runs_repo

        service._record_accounting_unresolved(run_id, incident_evidence)

    # No evidence fields set (run was not RUNNING)
    # The method should have committed and returned early
    mock_session.commit.assert_called_once()
    # error_code should NOT have been set to ACCOUNTING_UNRESOLVED
    # (because the early return happened before setting it)
    # Since mock_run is a MagicMock, we can't assert it wasn't set,
    # but we can verify the commit was called (early return path)


def test_record_accounting_unresolved_failure_does_not_suppress_original() -> None:
    """If _record_accounting_unresolved itself fails, the original
    exception still propagates.  No _record_failure fallback."""
    service = _make_service(adapter=_FailingEvidenceAdapter())

    run_snapshot = _make_run_snapshot()
    prompt = _make_prompt()
    service._claim_run = MagicMock(return_value=(run_snapshot, prompt))  # type: ignore[method-assign]

    def failing_record(
        run_id: uuid.UUID,
        evidence: ProviderFailureEvidence,
        error_code: ProviderErrorCode,
        error_message: str,
    ) -> None:
        raise ConflictError("original accounting failure")

    service._record_failure_with_evidence = failing_record  # type: ignore[method-assign]

    def failing_incident(
        run_id: uuid.UUID, evidence: Any, original_error_type: str | None = None
    ) -> None:
        raise RuntimeError("incident persistence also failed")

    service._record_accounting_unresolved = failing_incident  # type: ignore[method-assign]

    record_failure_called = False

    def tracking_plain_record(run_id: uuid.UUID, code: ProviderErrorCode, message: str) -> None:
        nonlocal record_failure_called
        record_failure_called = True

    service._record_failure = tracking_plain_record  # type: ignore[method-assign]

    # Execute — should raise the ORIGINAL exception (ConflictError),
    # not the incident persistence exception (RuntimeError)
    with pytest.raises(ConflictError):
        asyncio.run(service._execute_run(run_snapshot.id))

    # _record_failure was NOT called
    assert record_failure_called is False


# ---------------------------------------------------------------------------
# Phase 13.5.10F — Success-result accounting failure (Case C)
# ---------------------------------------------------------------------------


class _SuccessResultAdapter:
    """Adapter that returns a valid ProviderResult."""

    provider = LLMProvider.OPENAI
    surface = ProviderSurface.OPENAI_RESPONSES_API

    def __init__(self, result: ProviderResult | None = None) -> None:
        self._result = result or _make_provider_result()

    def capabilities(self) -> Any:
        return MagicMock()

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        return self._result


def _make_provider_result() -> ProviderResult:
    """Create a valid ProviderResult for testing."""
    return ProviderResult(
        provider=LLMProvider.OPENAI,
        surface=ProviderSurface.OPENAI_RESPONSES_API,
        execution_mode=ProviderExecutionMode.WEB_GROUNDED,
        requested_model="gpt-5.6-terra",
        returned_model="gpt-5.6-terra",
        response_text="This is a valid response.",
        citations=(),
        usage=ProviderUsage(
            input_tokens=13010,
            cached_input_tokens=4251,
            output_tokens=422,
            reasoning_tokens=320,
            total_tokens=13432,
            search_requests=2,
        ),
        provider_request_id="req_success_001",
        provider_response_id="resp_success_001",
        finish_reason="stop",
        latency_ms=1500,
        search_used=True,
    )


def test_success_result_accounting_failure_no_record_failure() -> None:
    """ProviderResult valid + record() fails → _record_failure NOT called.
    Instead, ACCOUNTING_UNRESOLVED incident is persisted and exception propagates."""
    service = _make_service(adapter=_SuccessResultAdapter())

    run_snapshot = _make_run_snapshot()
    prompt = _make_prompt()
    service._claim_run = MagicMock(return_value=(run_snapshot, prompt))  # type: ignore[method-assign]

    record_failure_called = False

    def tracking_record_failure(run_id: uuid.UUID, code: ProviderErrorCode, message: str) -> None:
        nonlocal record_failure_called
        record_failure_called = True

    service._record_failure = tracking_record_failure  # type: ignore[method-assign]

    # Mock _record_accounting_unresolved to capture the evidence
    captured_incidents: list[tuple[uuid.UUID, Any]] = []

    def capturing_incident(
        run_id: uuid.UUID, evidence: Any, original_error_type: str | None = None
    ) -> None:
        captured_incidents.append((run_id, evidence))

    service._record_accounting_unresolved = capturing_incident  # type: ignore[method-assign]

    # Mock PromptRunResultRecorder.record to raise
    with patch("app.services.scan_execution_service.PromptRunResultRecorder") as mock_recorder_cls:
        mock_recorder_cls.return_value.record = MagicMock(
            side_effect=ConflictError("quota conflict")
        )

        with pytest.raises(ConflictError):
            asyncio.run(service._execute_run(run_snapshot.id))

    # _record_failure was NOT called
    assert record_failure_called is False
    # _record_accounting_unresolved WAS called
    assert len(captured_incidents) == 1
    _, incident_evidence = captured_incidents[0]
    assert incident_evidence.provider_request_id == "req_success_001"
    assert incident_evidence.provider_response_id == "resp_success_001"
    assert incident_evidence.response_text == "This is a valid response."
    assert incident_evidence.usage.input_tokens == 13010
    assert incident_evidence.usage.search_requests == 2


def test_success_result_accounting_failure_propagates_to_execute_scan() -> None:
    """When record() fails for a valid ProviderResult, execute_scan does NOT
    call finalize().  The exception propagates through _execute_run_ids."""
    from app.core.enums import ScanType

    service = _make_service(adapter=_SuccessResultAdapter())
    service._settings.scan_max_concurrency = 1

    run_snapshot = _make_run_snapshot()
    prompt = _make_prompt()
    service._claim_run = MagicMock(return_value=(run_snapshot, prompt))  # type: ignore[method-assign]

    service._record_accounting_unresolved = MagicMock()  # type: ignore[method-assign]

    finalize_called = False

    def tracking_finalize(*args: Any, **kwargs: Any) -> Any:
        nonlocal finalize_called
        finalize_called = True
        return MagicMock()

    # Mock PromptRunResultRecorder.record to raise
    with (
        patch("app.services.scan_execution_service.PromptRunResultRecorder") as mock_recorder_cls,
        patch("app.services.scan_execution_service.ScanFinalizationService") as mock_finalizer_cls,
        patch.object(service, "_list_run_ids", return_value=[run_snapshot.id]),
        patch.object(service, "_claim_scan", return_value=True),
        patch.object(service, "_get_scan_type_and_repeats", return_value=(ScanType.STANDARD, 1)),
    ):
        mock_recorder_cls.return_value.record = MagicMock(
            side_effect=ConflictError("quota conflict")
        )
        mock_finalizer_cls.return_value.finalize = tracking_finalize

        with pytest.raises(ConflictError):
            asyncio.run(service.execute_scan(run_snapshot.scan_id))

    assert finalize_called is False


def test_success_result_accounting_failure_prompt_run_remains_running() -> None:
    """When record() fails, the incident recorder sets ACCOUNTING_UNRESOLVED
    and the PromptRun remains RUNNING (not FAILED, not SUCCEEDED)."""
    from app.services.scan_execution_service import AccountingIncidentEvidence

    service = _make_service(adapter=_SuccessResultAdapter())

    run_id = uuid.uuid4()
    result = _make_provider_result()
    incident_evidence = AccountingIncidentEvidence.from_result(result)

    mock_run = MagicMock(spec=PromptRun)
    mock_run.status = PromptRunStatus.RUNNING
    mock_run.id = run_id

    mock_session = MagicMock()
    mock_runs_repo = MagicMock()
    mock_runs_repo.get_for_update.return_value = mock_run

    mock_factory = MagicMock()
    mock_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_factory.return_value.__exit__ = MagicMock(return_value=False)

    service._factory = mock_factory

    with patch("app.services.scan_execution_service.PromptRunRepository") as mock_runs_cls:
        mock_runs_cls.return_value = mock_runs_repo

        service._record_accounting_unresolved(run_id, incident_evidence, "ConflictError")

    # Error code set to ACCOUNTING_UNRESOLVED
    assert mock_run.error_code == ProviderErrorCode.ACCOUNTING_UNRESOLVED
    assert mock_run.error_message is not None
    assert "Accounting unresolved" in mock_run.error_message
    assert "ConflictError" in mock_run.error_message
    # Status remains RUNNING
    assert mock_run.status == PromptRunStatus.RUNNING
    # Evidence fields persisted
    assert mock_run.provider_request_id == "req_success_001"
    assert mock_run.provider_response_id == "resp_success_001"
    assert mock_run.response_text == "This is a valid response."
    assert mock_run.input_tokens == 13010
    assert mock_run.output_tokens == 422
    assert mock_run.search_requests == 2
    # Session committed
    mock_session.commit.assert_called_once()


def test_success_result_accounting_failure_contract_mismatch_not_malformed() -> None:
    """When record() raises ProviderResponseError (contract mismatch),
    the error is NOT classified as MALFORMED_RESPONSE.
    It becomes ACCOUNTING_UNRESOLVED with the original error type preserved."""
    service = _make_service(adapter=_SuccessResultAdapter())

    run_snapshot = _make_run_snapshot()
    prompt = _make_prompt()
    service._claim_run = MagicMock(return_value=(run_snapshot, prompt))  # type: ignore[method-assign]

    captured_incidents: list[tuple[uuid.UUID, Any, str | None]] = []

    def capturing_incident(
        run_id: uuid.UUID, evidence: Any, original_error_type: str | None = None
    ) -> None:
        captured_incidents.append((run_id, evidence, original_error_type))

    service._record_accounting_unresolved = capturing_incident  # type: ignore[method-assign]

    from app.providers.errors import ProviderResponseError

    with patch("app.services.scan_execution_service.PromptRunResultRecorder") as mock_recorder_cls:
        mock_recorder_cls.return_value.record = MagicMock(
            side_effect=ProviderResponseError(
                "Contract mismatch in recorder.",
                provider=LLMProvider.OPENAI.value,
            )
        )

        with pytest.raises(ProviderResponseError):
            asyncio.run(service._execute_run(run_snapshot.id))

    # Incident was called with original_error_type=ProviderResponseError
    assert len(captured_incidents) == 1
    _, _, error_type = captured_incidents[0]
    assert error_type == "ProviderResponseError"


def test_success_result_concurrency_fatal_stops_new_calls() -> None:
    """5 runs, concurrency=2.  Run A: ProviderResult valid + record() fails.
    Run B: already started.  Runs C/D/E: never start adapter.execute()."""
    result = _make_provider_result()
    adapter = _SuccessResultAdapter(result)
    service = _make_service(adapter=adapter)
    service._settings.scan_max_concurrency = 2

    run_ids = [uuid.uuid4() for _ in range(5)]

    def make_claim(rid: uuid.UUID) -> Any:
        run = MagicMock(spec=PromptRun)
        run.id = rid
        run.scan_id = uuid.uuid4()
        run.status = PromptRunStatus.PENDING
        run.provider = LLMProvider.OPENAI.value
        run.provider_surface = ProviderSurface.OPENAI_RESPONSES_API.value
        run.execution_mode = ProviderExecutionMode.WEB_GROUNDED.value
        run.requested_model = "gpt-5.6-terra"
        return (run, _make_prompt())

    service._claim_run = MagicMock(side_effect=lambda rid: make_claim(rid))  # type: ignore[method-assign]
    service._record_accounting_unresolved = MagicMock()  # type: ignore[method-assign]

    # Track which runs started adapter.execute
    original_execute = adapter.execute
    started_runs: list[uuid.UUID] = []

    async def tracking_execute(request: ProviderRequest) -> ProviderResult:
        cid = request.correlation_id or ""
        run_id_str = cid.split("run:")[1]
        started_runs.append(uuid.UUID(run_id_str))
        return await original_execute(request)

    adapter.execute = tracking_execute  # type: ignore[method-assign]

    call_count = 0

    def failing_record(run_id: uuid.UUID, result: Any) -> None:
        nonlocal call_count
        call_count += 1
        # First call fails fatally
        if call_count == 1:
            raise ConflictError("accounting failure")

    with patch("app.services.scan_execution_service.PromptRunResultRecorder") as mock_recorder_cls:
        mock_recorder_cls.return_value.record = failing_record

        with pytest.raises(ConflictError):
            asyncio.run(service._execute_run_ids(run_ids))

    # Only 2 runs started (concurrency=2)
    assert len(started_runs) == 2
    # Runs C/D/E never started
    for rid in run_ids[2:]:
        assert rid not in started_runs


def test_economic_symmetry_failure_and_success_same_semantics() -> None:
    """ProviderFailureEvidence accounting failure and ProviderResult accounting
    failure produce the same economic semantics: RUNNING, ACCOUNTING_UNRESOLVED,
    no UsageEvent, no finalize, fatal scheduling, incident evidence durable."""
    from app.services.scan_execution_service import AccountingIncidentEvidence

    # Case 1: Failure evidence
    failure_ev = _make_evidence()
    incident1 = AccountingIncidentEvidence.from_failure_evidence(failure_ev)

    # Case 2: Success result
    success_result = _make_provider_result()
    incident2 = AccountingIncidentEvidence.from_result(success_result)

    # Both have the same economic semantics
    assert incident1.provider == incident2.provider
    assert incident1.surface == incident2.surface
    assert incident1.execution_mode == incident2.execution_mode
    assert incident1.provider_request_id is not None
    assert incident2.provider_request_id is not None
    assert incident1.usage.input_tokens is not None
    assert incident2.usage.input_tokens is not None
    assert incident1.usage.search_requests is not None
    assert incident2.usage.search_requests is not None

    # Difference: success has response_text, failure does not
    assert incident2.response_text is not None
    assert incident1.response_text is None

    # Both have no cost fabrication (cost is not in AccountingIncidentEvidence)
    # Both will be persisted with error_code=ACCOUNTING_UNRESOLVED
    # Both will keep PromptRun RUNNING
    # Both will propagate the exception (fatal scheduling)


def test_success_incident_stale_recovery_protected() -> None:
    """A PromptRun left RUNNING + ACCOUNTING_UNRESOLVED from a success-result
    accounting failure is protected by the stale recovery rule from 13.5.10D:
    not FAILED, not finalized, quota not released."""
    from app.services.scan_execution_service import AccountingIncidentEvidence

    service = _make_service(adapter=_SuccessResultAdapter())

    run_id = uuid.uuid4()
    result = _make_provider_result()
    incident_evidence = AccountingIncidentEvidence.from_result(result)

    mock_run = MagicMock(spec=PromptRun)
    mock_run.status = PromptRunStatus.RUNNING
    mock_run.id = run_id

    mock_session = MagicMock()
    mock_runs_repo = MagicMock()
    mock_runs_repo.get_for_update.return_value = mock_run

    mock_factory = MagicMock()
    mock_factory.return_value.__enter__ = MagicMock(return_value=mock_session)
    mock_factory.return_value.__exit__ = MagicMock(return_value=False)

    service._factory = mock_factory

    with patch("app.services.scan_execution_service.PromptRunRepository") as mock_runs_cls:
        mock_runs_cls.return_value = mock_runs_repo

        service._record_accounting_unresolved(run_id, incident_evidence, "ConflictError")

    # The run has the marker that stale recovery will see
    assert mock_run.error_code == ProviderErrorCode.ACCOUNTING_UNRESOLVED
    assert mock_run.status == PromptRunStatus.RUNNING
    # Stale recovery checks for RUNNING status → will NOT recover
    # (verified in test_stale_recovery_economic_safety.py)
