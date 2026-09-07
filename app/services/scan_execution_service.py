"""Bounded, no-retry execution of an already-reserved Scan snapshot."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.core.enums import (
    LLMProvider,
    PromptRunStatus,
    ProviderErrorCode,
    ProviderExecutionMode,
    ProviderSurface,
    QuotaReservationStatus,
    ScanStatus,
    ScanType,
)
from app.core.logging import get_logger
from app.models.quota_reservation import QuotaReservation
from app.models.scan import PromptRun, ResponseSource, Scan
from app.models.tracking import Prompt
from app.providers.base import (
    ProviderCitation,
    ProviderFailureEvidence,
    ProviderRequest,
    ProviderResult,
    ProviderUsage,
)
from app.providers.errors import ProviderError
from app.providers.registry import ProviderRegistry
from app.repositories.scan_repository import (
    PromptRunRepository,
    ResponseSourceRepository,
    ScanRepository,
)
from app.services.audit_service import AuditService
from app.services.prompt_run_result_recorder import PromptRunResultRecorder
from app.services.scan_finalization_service import ScanFinalizationService
from app.services.scanning.errors import (
    enrich_error_message,
    map_provider_error,
    safe_error_message,
)

logger = get_logger("app.scan_execution")


@dataclass(frozen=True)
class AccountingIncidentEvidence:
    """Normalized evidence for ACCOUNTING_UNRESOLVED incident persistence.

    Common shape extracted from either ProviderResult (success path) or
    ProviderFailureEvidence (failure path) so that a single
    _record_accounting_unresolved implementation can persist both.
    """

    provider: LLMProvider
    surface: ProviderSurface
    execution_mode: ProviderExecutionMode
    requested_model: str
    returned_model: str | None
    provider_request_id: str | None
    provider_response_id: str | None
    latency_ms: int
    search_used: bool
    usage: ProviderUsage
    response_text: str | None = None
    citations: tuple[ProviderCitation, ...] = field(default_factory=tuple)
    incomplete_reason: str | None = None
    requested_max_tool_calls: int | None = None
    observed_search_requests: int | None = None
    observed_web_tool_call_count: int | None = None

    @classmethod
    def from_result(cls, result: ProviderResult) -> AccountingIncidentEvidence:
        """Normalize a ProviderResult into incident evidence."""
        return cls(
            provider=result.provider,
            surface=result.surface,
            execution_mode=result.execution_mode,
            requested_model=result.requested_model,
            returned_model=result.returned_model,
            provider_request_id=result.provider_request_id,
            provider_response_id=result.provider_response_id,
            latency_ms=result.latency_ms,
            search_used=result.search_used,
            usage=result.usage,
            response_text=result.response_text,
            citations=result.citations,
        )

    @classmethod
    def from_failure_evidence(cls, evidence: ProviderFailureEvidence) -> AccountingIncidentEvidence:
        """Normalize ProviderFailureEvidence into incident evidence."""
        return cls(
            provider=evidence.provider,
            surface=evidence.surface,
            execution_mode=evidence.execution_mode,
            requested_model=evidence.requested_model,
            returned_model=evidence.returned_model,
            provider_request_id=evidence.provider_request_id,
            provider_response_id=evidence.provider_response_id,
            latency_ms=evidence.latency_ms,
            search_used=evidence.search_used,
            usage=evidence.usage,
            response_text=None,
            citations=evidence.citations,
            incomplete_reason=evidence.incomplete_reason,
            requested_max_tool_calls=evidence.requested_max_tool_calls,
            observed_search_requests=evidence.observed_search_requests,
            observed_web_tool_call_count=evidence.observed_web_tool_call_count,
        )


class ScanExecutionService:
    """Execute each planned PromptRun once with bounded async concurrency.

    For CONFIDENCE scans, runs are executed round-by-round: all
    observation_index=1 runs finish before observation_index=2 begins.
    Within each round, bounded concurrency is used.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        registry: ProviderRegistry | None = None,
        settings: Settings | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._factory = session_factory
        self._registry = registry or ProviderRegistry()
        self._settings = settings or get_settings()
        self._audit = audit_service

    async def execute_scan(self, scan_id: uuid.UUID) -> bool:
        if not self._claim_scan(scan_id):
            return False

        # Determine scan type to choose execution strategy.
        scan_type, repeat_count = self._get_scan_type_and_repeats(scan_id)

        if scan_type == ScanType.CONFIDENCE and repeat_count > 1:
            await self._execute_confidence_rounds(scan_id, repeat_count)
        else:
            await self._execute_standard_round(scan_id)

        with self._factory() as session:
            ScanFinalizationService(
                session,
                self._audit,
                analysis_session_factory=self._factory,
            ).finalize(scan_id, trigger_analysis=True)
        return True

    def _get_scan_type_and_repeats(self, scan_id: uuid.UUID) -> tuple[ScanType, int]:
        with self._factory() as session:
            scan = session.get(Scan, scan_id)
            if scan is None:
                return ScanType.STANDARD, 1
            return scan.scan_type, scan.repeat_count

    async def _execute_standard_round(self, scan_id: uuid.UUID) -> None:
        """Execute all runs with bounded concurrency (STANDARD behavior)."""
        run_ids = self._list_run_ids(scan_id)
        await self._execute_run_ids(run_ids)

    async def _execute_confidence_rounds(self, scan_id: uuid.UUID, repeat_count: int) -> None:
        """Execute runs round-by-round.

        For each observation_index from 1 to repeat_count:
        - Gather all run IDs for that round.
        - Execute them with bounded concurrency.
        - Wait for the round to finish before starting the next.

        This reduces accidental burst correlation and prevents sending
        the same Prompt x Provider multiple times simultaneously.
        """
        for obs_idx in range(1, repeat_count + 1):
            run_ids = self._list_run_ids_by_observation(scan_id, obs_idx)
            if not run_ids:
                continue
            await self._execute_run_ids(run_ids)

    async def _execute_run_ids(self, run_ids: list[uuid.UUID]) -> None:
        """Execute runs with bounded concurrency and economic fail-closed.

        Uses explicit task creation + asyncio.wait(FIRST_COMPLETED) instead of
        asyncio.gather to ensure:

        - At most ``scan_max_concurrency`` provider calls are in-flight.
        - When a fatal accounting error occurs, NO new provider calls start.
        - Already-started provider calls are NOT cancelled (they may have
          been billed by the provider).  They are awaited and their results
          are processed normally.
        - After all in-flight tasks complete, the fatal error is propagated.
        - No background tasks remain after this method returns/raises.
        """
        from collections import deque

        max_concurrency = self._settings.scan_max_concurrency
        pending = deque(run_ids)
        active: set[asyncio.Task[None]] = set()
        fatal_errors: list[BaseException] = []
        stop_scheduling = False

        # Fill up to max_concurrency initially.
        while not stop_scheduling and pending and len(active) < max_concurrency:
            run_id = pending.popleft()
            task = asyncio.create_task(self._execute_run(run_id))
            active.add(task)

        while active:
            done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)

            for task in done:
                exc = task.exception()
                if exc is not None:
                    # Fatal accounting/infrastructure error.
                    # Stop scheduling new calls; do NOT cancel in-flight tasks.
                    fatal_errors.append(exc)
                    stop_scheduling = True

            # Refill only if no fatal error has been observed.
            if not stop_scheduling:
                while pending and len(active) < max_concurrency:
                    run_id = pending.popleft()
                    task = asyncio.create_task(self._execute_run(run_id))
                    active.add(task)

        # All in-flight tasks have completed.  Propagate fatal errors.
        if fatal_errors:
            raise fatal_errors[0]

    def _claim_scan(self, scan_id: uuid.UUID) -> bool:
        with self._factory() as session:
            scans = ScanRepository(session)
            runs = PromptRunRepository(session)
            scan = scans.get_for_update(scan_id)
            if scan is None:
                session.rollback()
                return False
            if scan.status != ScanStatus.PENDING:
                session.commit()
                return False
            if scan.quota_reservation_id is None:
                self._reject_scan_before_execution(
                    session,
                    scan,
                    runs,
                    failure_code="MISSING_QUOTA_RESERVATION",
                    failure_message="Scan cannot execute without quota reservation.",
                )
                return False
            reservation = session.get(QuotaReservation, scan.quota_reservation_id)
            if reservation is None or reservation.status not in (
                QuotaReservationStatus.ACTIVE,
                QuotaReservationStatus.COMMITTED,
            ):
                self._reject_scan_before_execution(
                    session,
                    scan,
                    runs,
                    failure_code="INVALID_QUOTA_RESERVATION",
                    failure_message="Scan quota reservation is not active.",
                )
                return False
            scan.status = ScanStatus.RUNNING
            scan.started_at = datetime.now(UTC)
            session.commit()
            workspace_id = scan.workspace_id
        if self._audit is not None:
            self._audit.record(
                action="SCAN_STARTED",
                workspace_id=workspace_id,
                entity_type="scan",
                entity_id=scan_id,
            )
        return True

    def _reject_scan_before_execution(
        self,
        session: Session,
        scan: Scan,
        runs: PromptRunRepository,
        *,
        failure_code: str,
        failure_message: str,
    ) -> None:
        """Terminalize a Scan rejected before any provider call.

        Marks every unresolved PromptRun FAILED with an internal/accounting
        error code, records the failure reason on the Scan, and then
        atomically finalizes (classifying counts, setting terminal status,
        and releasing unused quota) so the invariant ``terminal Scan →
        zero unresolved PromptRuns`` always holds. No provider is invoked.
        """
        completed_at = datetime.now(UTC)
        runs.mark_unresolved_failed(
            scan.id,
            completed_at=completed_at,
            error_message=failure_message,
            error_code=ProviderErrorCode.ACCOUNTING_ERROR,
        )
        # Record the rejection reason; finalize() will set status/counts/
        # completed_at atomically with quota release.
        scan.failure_code = failure_code
        scan.failure_message = failure_message
        session.commit()
        # Finalize in a fresh session to atomically classify counts and
        # release any remaining reserved quota.
        with self._factory() as finalize_session:
            ScanFinalizationService(finalize_session, self._audit).finalize(
                scan.id, trigger_analysis=False
            )

    def _list_run_ids(self, scan_id: uuid.UUID) -> list[uuid.UUID]:
        with self._factory() as session:
            return PromptRunRepository(session).list_ids_by_scan(scan_id)

    def _list_run_ids_by_observation(
        self, scan_id: uuid.UUID, observation_index: int
    ) -> list[uuid.UUID]:
        """List run IDs for a specific observation round in deterministic order."""
        with self._factory() as session:
            return list(
                session.execute(
                    select(PromptRun.id)
                    .where(
                        PromptRun.scan_id == scan_id,
                        PromptRun.observation_index == observation_index,
                    )
                    .order_by(PromptRun.created_at, PromptRun.id)
                ).scalars()
            )

    async def _execute_run(self, run_id: uuid.UUID) -> None:
        claimed = self._claim_run(run_id)
        if claimed is None:
            return
        run_snapshot, prompt = claimed
        request = ProviderRequest(
            prompt=prompt.text,
            mode=ProviderExecutionMode(run_snapshot.execution_mode),
            model=run_snapshot.requested_model,
            target_country=prompt.target_country,
            target_language=prompt.target_language,
            correlation_id=f"scan:{run_snapshot.scan_id}:run:{run_snapshot.id}",
        )
        try:
            adapter = self._registry.get(LLMProvider(run_snapshot.provider))
            result = await adapter.execute(request)
        except ProviderError as exc:
            # Case B: provider returned a billable response envelope but the
            # functional result is unusable (empty text, incomplete, missing
            # search, max_tool_calls violation).  The exception carries
            # ProviderFailureEvidence — persist it atomically with usage/cost
            # and commit one AI Check to quota.
            if exc.evidence is not None:
                self._record_failure_with_evidence(
                    run_id,
                    exc.evidence,
                    map_provider_error(exc),
                    enrich_error_message(safe_error_message(exc), exc.evidence),
                )
                return
            # Case A: error before any billable response (config, auth, 429,
            # timeout, 5xx, transport).  No usage, no cost, no AI Check.
            self._record_failure(run_id, map_provider_error(exc), safe_error_message(exc))
            return
        except Exception as exc:
            logger.exception(
                "prompt_run_internal_failure",
                prompt_run_id=str(run_id),
                error_type=type(exc).__name__,
            )
            self._record_failure(
                run_id,
                ProviderErrorCode.INTERNAL_ERROR,
                "Internal scan execution failure.",
            )
            return

        try:
            with self._factory() as session:
                PromptRunResultRecorder(session).record(run_id, result)
        except Exception as exc:
            # Case C: ProviderResult was obtained (billable response), but
            # durable accounting failed.  This is NOT a provider failure —
            # it is an accounting incident.  Persist ACCOUNTING_UNRESOLVED
            # in a new session, keep the PromptRun RUNNING, and propagate
            # the exception so the scheduler marks fatal state and
            # finalize() is never called.
            logger.exception(
                "prompt_run_success_accounting_failure",
                prompt_run_id=str(run_id),
                error_type=type(exc).__name__,
                provider=result.provider.value,
                requested_model=result.requested_model,
                provider_request_id=result.provider_request_id,
                provider_response_id=result.provider_response_id,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                reasoning_tokens=result.usage.reasoning_tokens,
                search_requests=result.usage.search_requests,
            )
            self._record_accounting_unresolved(
                run_id,
                AccountingIncidentEvidence.from_result(result),
                original_error_type=type(exc).__name__,
            )
            raise

    def _record_failure_with_evidence(
        self,
        run_id: uuid.UUID,
        evidence: ProviderFailureEvidence,
        error_code: ProviderErrorCode,
        error_message: str,
    ) -> None:
        """Persist billable failure evidence (case B).

        Delegates to PromptRunResultRecorder.record_failure_evidence in a
        fresh session.  If the recorder fails (e.g. pricing rule missing,
        quota conflict), an ACCOUNTING_UNRESOLVED incident marker is
        persisted in a separate session — preserving as much evidence as
        possible without faking quota commit or UsageEvent.  The PromptRun
        remains RUNNING with error_code=ACCOUNTING_UNRESOLVED.

        The original exception is then re-raised so it propagates to
        execute_scan and prevents finalization.
        """
        try:
            with self._factory() as session:
                PromptRunResultRecorder(session).record_failure_evidence(
                    run_id, evidence, error_code=error_code, error_message=error_message
                )
        except Exception:
            # Sanitized structured log for operational evidence.
            logger.exception(
                "prompt_run_failure_evidence_accounting_failure",
                prompt_run_id=str(run_id),
                provider=evidence.provider.value,
                requested_model=evidence.requested_model,
                provider_request_id=evidence.provider_request_id,
                provider_response_id=evidence.provider_response_id,
                input_tokens=evidence.usage.input_tokens,
                output_tokens=evidence.usage.output_tokens,
                reasoning_tokens=evidence.usage.reasoning_tokens,
                search_requests=evidence.usage.search_requests,
                web_tool_call_count=evidence.usage.web_tool_call_count,
                search_action_count=evidence.usage.search_action_count,
                open_page_action_count=evidence.usage.open_page_action_count,
                find_in_page_action_count=evidence.usage.find_in_page_action_count,
                unknown_web_action_count=evidence.usage.unknown_web_action_count,
                incomplete_reason=evidence.incomplete_reason,
                requested_max_tool_calls=evidence.requested_max_tool_calls,
                observed_search_requests=evidence.observed_search_requests,
                observed_web_tool_call_count=evidence.observed_web_tool_call_count,
            )
            # Attempt to persist an ACCOUNTING_UNRESOLVED incident marker
            # in a separate session.  This preserves evidence without
            # faking quota commit or UsageEvent.
            self._record_accounting_unresolved(
                run_id,
                AccountingIncidentEvidence.from_failure_evidence(evidence),
            )
            # Re-raise the original exception so it propagates and
            # prevents finalization.
            raise

    def _record_accounting_unresolved(
        self,
        run_id: uuid.UUID,
        evidence: AccountingIncidentEvidence,
        original_error_type: str | None = None,
    ) -> None:
        """Persist an ACCOUNTING_UNRESOLVED incident marker.

        Uses a fresh session (separate from the failed recorder session).
        Persists available evidence fields on the PromptRun WITHOUT:
        - creating a UsageEvent
        - committing quota
        - faking cost_source or pricing_rule_id
        - marking the run SUCCEEDED or terminally FAILED

        The PromptRun remains RUNNING with error_code=ACCOUNTING_UNRESOLVED
        so that stale recovery can distinguish this from an ordinary crash.

        Citations are persisted idempotently: existing (prompt_run_id, ordinal)
        pairs are skipped to avoid IntegrityError on future reconciliation.
        """
        try:
            with self._factory() as session:
                runs = PromptRunRepository(session)
                run = runs.get_for_update(run_id)
                if run is None or run.status != PromptRunStatus.RUNNING:
                    session.commit()
                    return
                # Persist available evidence without faking accounting.
                run.provider_request_id = evidence.provider_request_id
                run.provider_response_id = evidence.provider_response_id
                run.returned_model = evidence.returned_model
                run.latency_ms = evidence.latency_ms
                run.search_used = evidence.search_used
                run.input_tokens = evidence.usage.input_tokens
                run.output_tokens = evidence.usage.output_tokens
                run.total_tokens = evidence.usage.total_tokens
                run.cached_input_tokens = evidence.usage.cached_input_tokens
                run.cache_write_input_tokens = evidence.usage.cache_write_input_tokens
                run.reasoning_tokens = evidence.usage.reasoning_tokens
                run.citation_tokens = evidence.usage.citation_tokens
                run.search_requests = evidence.usage.search_requests
                run.web_tool_call_count = evidence.usage.web_tool_call_count
                run.search_action_count = evidence.usage.search_action_count
                run.open_page_action_count = evidence.usage.open_page_action_count
                run.find_in_page_action_count = evidence.usage.find_in_page_action_count
                run.unknown_web_action_count = evidence.usage.unknown_web_action_count
                # Persist response_text if available (from ProviderResult).
                if evidence.response_text is not None:
                    run.response_text = evidence.response_text
                # Do NOT set cost fields — cost was not durably calculated.
                # Do NOT set usage_event_id — no UsageEvent was created.
                # Do NOT set pricing_rule_id — pricing was not resolved.
                run.error_code = ProviderErrorCode.ACCOUNTING_UNRESOLVED
                # Build sanitized deterministic error message.
                msg = (
                    "Accounting unresolved after provider response. Manual reconciliation required."
                )
                if original_error_type:
                    msg = f"{msg} original_error_type={original_error_type}."
                run.error_message = msg[:1000]
                # PromptRun.status remains RUNNING — not terminalized.

                # Persist citations idempotently: skip existing ordinals.
                if evidence.citations:
                    self._persist_citations_idempotent(session, run.id, evidence.citations)

                session.commit()
        except Exception:
            # If incident persistence also fails, log and continue.
            # The original exception will propagate regardless.
            logger.exception(
                "accounting_unresolved_incident_persistence_failure",
                prompt_run_id=str(run_id),
                provider=evidence.provider.value,
                provider_request_id=evidence.provider_request_id,
                provider_response_id=evidence.provider_response_id,
            )

    def _persist_citations_idempotent(
        self,
        session: Session,
        run_id: uuid.UUID,
        citations: tuple[ProviderCitation, ...],
    ) -> None:
        """Persist ResponseSource rows idempotently via the shared authority.

        Delegates to ResponseSourceRepository.create_batch_idempotent which
        handles:
        - skip existing (prompt_run_id, ordinal) with same URL
        - raise ConflictError on URL mismatch (evidence conflict)
        - create new ordinals
        """
        sources = [
            ResponseSource(
                prompt_run_id=run_id,
                ordinal=ordinal,
                url=citation.url,
                title=citation.title,
                source_type=citation.source_type,
                start_index=citation.start_index,
                end_index=citation.end_index,
                cited_text=citation.cited_text,
            )
            for ordinal, citation in enumerate(citations, start=1)
        ]
        ResponseSourceRepository(session).create_batch_idempotent(sources)

    def _claim_run(self, run_id: uuid.UUID) -> tuple[PromptRun, Prompt] | None:
        with self._factory() as session:
            runs = PromptRunRepository(session)
            run = runs.get_for_update(run_id)
            if run is None or run.status != PromptRunStatus.PENDING:
                session.commit()
                return None
            run.status = PromptRunStatus.RUNNING
            run.started_at = datetime.now(UTC)
            prompt = session.get(Prompt, run.prompt_id)
            if prompt is None:
                run.status = PromptRunStatus.FAILED
                run.error_code = ProviderErrorCode.INTERNAL_ERROR
                run.error_message = "Snapshotted prompt is unavailable."
                run.completed_at = datetime.now(UTC)
                session.commit()
                return None
            session.commit()
            session.expunge(run)
            session.expunge(prompt)
            return run, prompt

    def _record_failure(self, run_id: uuid.UUID, code: ProviderErrorCode, message: str) -> None:
        with self._factory() as session:
            run = PromptRunRepository(session).get_for_update(run_id)
            if run is None or run.status != PromptRunStatus.RUNNING:
                session.commit()
                return
            run.status = PromptRunStatus.FAILED
            run.error_code = code
            run.error_message = message[:1000]
            run.completed_at = datetime.now(UTC)
            session.commit()
            logger.warning(
                "prompt_run_failed",
                scan_id=str(run.scan_id),
                prompt_run_id=str(run.id),
                provider=str(run.provider),
                surface=str(run.provider_surface),
                mode=str(run.execution_mode),
                requested_model=run.requested_model,
                error_code=code.value,
            )
