"""Bounded, no-retry execution of an already-reserved Scan snapshot."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.core.enums import (
    LLMProvider,
    PromptRunStatus,
    ProviderErrorCode,
    ProviderExecutionMode,
    QuotaReservationStatus,
    ScanStatus,
)
from app.core.logging import get_logger
from app.models.quota_reservation import QuotaReservation
from app.models.scan import PromptRun
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
    """Execute each planned PromptRun once with bounded async concurrency."""

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
        run_ids = self._list_run_ids(scan_id)
        semaphore = asyncio.Semaphore(self._settings.scan_max_concurrency)

        async def bounded(run_id: uuid.UUID) -> None:
            async with semaphore:
                await self._execute_run(run_id)

        await asyncio.gather(*(bounded(run_id) for run_id in run_ids))
        with self._factory() as session:
            ScanFinalizationService(session, self._audit).finalize(scan_id)
        return True

    def _claim_scan(self, scan_id: uuid.UUID) -> bool:
        with self._factory() as session:
            scans = ScanRepository(session)
            scan = scans.get_for_update(scan_id)
            if scan is None:
                session.rollback()
                return False
            if scan.status != ScanStatus.PENDING:
                session.commit()
                return False
            if scan.quota_reservation_id is None:
                scan.status = ScanStatus.FAILED
                scan.failure_code = "MISSING_QUOTA_RESERVATION"
                scan.failure_message = "Scan cannot execute without quota reservation."
                scan.completed_at = datetime.now(UTC)
                session.commit()
                return False
            reservation = session.get(QuotaReservation, scan.quota_reservation_id)
            if reservation is None or reservation.status not in (
                QuotaReservationStatus.ACTIVE,
                QuotaReservationStatus.COMMITTED,
            ):
                scan.status = ScanStatus.FAILED
                scan.failure_code = "INVALID_QUOTA_RESERVATION"
                scan.failure_message = "Scan quota reservation is not active."
                scan.completed_at = datetime.now(UTC)
                session.commit()
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

    def _list_run_ids(self, scan_id: uuid.UUID) -> list[uuid.UUID]:
        with self._factory() as session:
            return PromptRunRepository(session).list_ids_by_scan(scan_id)

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
