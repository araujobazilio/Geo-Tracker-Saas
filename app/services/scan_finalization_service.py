"""Terminal scan classification, unused quota release, and stale recovery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.enums import PromptRunStatus, ProviderErrorCode, ScanStatus
from app.core.exceptions import ConflictError
from app.models.project import Project
from app.repositories.scan_repository import PromptRunRepository, ScanRepository
from app.services.audit_service import AuditService
from app.services.quota_service import QuotaService


class ScanFinalizationService:
    def __init__(self, session: Session, audit_service: AuditService | None = None) -> None:
        self._session = session
        self._audit = audit_service
        self._scans = ScanRepository(session)
        self._runs = PromptRunRepository(session)
        self._quota = QuotaService(session, audit_service=audit_service)

    def finalize(self, scan_id: uuid.UUID) -> ScanStatus:
        try:
            scan = self._scans.get_for_update(scan_id)
            if scan is None:
                raise ConflictError("Scan not found.")
            if scan.status in (ScanStatus.COMPLETED, ScanStatus.PARTIAL, ScanStatus.FAILED):
                self._session.commit()
                return scan.status
            succeeded, failed, unresolved = self._runs.terminal_counts(scan.id)
            if unresolved:
                raise ConflictError("Cannot finalize a Scan with unresolved PromptRuns.")
            completed_at = datetime.now(UTC)
            scan.successful_runs = succeeded
            scan.failed_runs = failed
            scan.completed_at = completed_at
            if succeeded == scan.planned_ai_checks:
                scan.status = ScanStatus.COMPLETED
            elif succeeded > 0:
                scan.status = ScanStatus.PARTIAL
            else:
                scan.status = ScanStatus.FAILED
            if succeeded > 0:
                project = self._session.get(Project, scan.project_id)
                if project is not None:
                    project.last_scan_at = completed_at
            status = scan.status
            reservation_id = scan.quota_reservation_id
            self._session.commit()
            if reservation_id is not None:
                self._quota.release_reservation(reservation_id)
            self._record_audit(scan.id, scan.workspace_id, status)
            return status
        except Exception:
            self._session.rollback()
            raise

    def _record_audit(
        self, scan_id: uuid.UUID, workspace_id: uuid.UUID, status: ScanStatus
    ) -> None:
        if self._audit is None:
            return
        action = {
            ScanStatus.COMPLETED: "SCAN_COMPLETED",
            ScanStatus.PARTIAL: "SCAN_PARTIAL",
            ScanStatus.FAILED: "SCAN_FAILED",
        }[status]
        self._audit.record(
            action=action,
            workspace_id=workspace_id,
            entity_type="scan",
            entity_id=scan_id,
        )


class ScanRecoveryService:
    """Fail stale unresolved work without repeating provider requests."""

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._scans = ScanRepository(session)
        self._runs = PromptRunRepository(session)
        self._finalizer = ScanFinalizationService(session, audit_service)

    def recover_stale_scans(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        before = current - timedelta(seconds=self._settings.scan_stale_after_seconds)
        scan_ids = [scan.id for scan in self._scans.list_stale_running(before)]
        recovered = 0
        for scan_id in scan_ids:
            try:
                scan = self._scans.get_for_update(scan_id)
                if (
                    scan is None
                    or scan.status != ScanStatus.RUNNING
                    or scan.started_at is None
                    or scan.started_at >= before
                ):
                    self._session.commit()
                    continue
                runs = self._runs.list_by_scan(scan.id)
                for run in runs:
                    if run.status in (PromptRunStatus.PENDING, PromptRunStatus.RUNNING):
                        run.status = PromptRunStatus.FAILED
                        run.error_code = ProviderErrorCode.INTERNAL_ERROR
                        run.error_message = "Worker stopped before evidence was durably recorded."
                        run.completed_at = current
                self._session.commit()
                self._finalizer.finalize(scan.id)
                recovered += 1
            except Exception:
                self._session.rollback()
                raise
        return recovered
