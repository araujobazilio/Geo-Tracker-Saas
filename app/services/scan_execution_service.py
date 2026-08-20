"""Bounded, no-retry execution of an already-reserved Scan snapshot."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.core.enums import (
    LLMProvider,
    PromptRunStatus,
    ProviderErrorCode,
    ProviderExecutionMode,
    QuotaReservationStatus,
    ScanStatus,
    ScanType,
)
from app.core.logging import get_logger
from app.models.quota_reservation import QuotaReservation
from app.models.scan import PromptRun, Scan
from app.models.tracking import Prompt
from app.providers.base import ProviderRequest
from app.providers.errors import ProviderError, ProviderResponseError
from app.providers.registry import ProviderRegistry
from app.repositories.scan_repository import PromptRunRepository, ScanRepository
from app.services.audit_service import AuditService
from app.services.prompt_run_result_recorder import PromptRunResultRecorder
from app.services.scan_finalization_service import ScanFinalizationService
from app.services.scanning.errors import map_provider_error, safe_error_message

logger = get_logger("app.scan_execution")


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
        semaphore = asyncio.Semaphore(self._settings.scan_max_concurrency)

        async def bounded(run_id: uuid.UUID) -> None:
            async with semaphore:
                await self._execute_run(run_id)

        await asyncio.gather(*(bounded(run_id) for run_id in run_ids))

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
        except ProviderResponseError as exc:
            self._record_failure(
                run_id,
                ProviderErrorCode.MALFORMED_RESPONSE,
                safe_error_message(exc),
            )
        except Exception as exc:
            logger.exception(
                "prompt_run_accounting_failure",
                prompt_run_id=str(run_id),
                error_type=type(exc).__name__,
            )
            self._record_failure(
                run_id,
                ProviderErrorCode.ACCOUNTING_ERROR,
                "Provider result could not be durably recorded.",
            )

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
