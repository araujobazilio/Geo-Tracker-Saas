"""Scheduled scan service — claims due schedules and triggers STANDARD scans.

Responsibilities:
- List/claim due schedules using FOR UPDATE SKIP LOCKED.
- Recheck current entitlement at execution time.
- Validate Project is active and ready.
- Detect conflicting active scheduled work.
- Create STANDARD Scan via ScanCreationService (no engine duplication).
- Record scheduler outcome and advance next_run_at.
- Preserve idempotency: deterministic slot identity.

PostgreSQL remains authoritative. Redis/Celery is transport only.
No catch-up storm: at most ONE due slot per scheduler evaluation.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    ProjectStatus,
    ScanStatus,
    ScanType,
    ScheduledScanOutcome,
)
from app.core.exceptions import ConflictError, NotFoundError, QuotaExceededError, ValidationError
from app.core.logging import get_logger
from app.models.project import Project
from app.models.project_scan_schedule import ProjectScanSchedule
from app.models.scan import Scan
from app.repositories.scan_repository import ScanRepository
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService
from app.services.scan_creation_service import ScanCreationService
from app.services.scanning.dispatcher import ScanDispatcher

logger = get_logger("app.scheduler")


@dataclass(frozen=True)
class SchedulerEvaluationResult:
    """Result of evaluating one due schedule."""

    schedule_id: uuid.UUID
    outcome: ScheduledScanOutcome
    scan_id: uuid.UUID | None = None
    skip_reason: str | None = None


class ScheduledScanService:
    """Claim due schedules and trigger STANDARD scans.

    This service does NOT execute provider requests — it only creates
    and dispatches Scans via the normal ScanCreationService. Provider
    execution continues via ``scan.execute`` Celery tasks.
    """

    def __init__(
        self,
        session: Session,
        dispatcher: ScanDispatcher,
        *,
        audit_service: AuditService | None = None,
    ) -> None:
        self._session = session
        self._dispatcher = dispatcher
        self._audit = audit_service
        self._entitlements = EntitlementService(session)
        self._scans = ScanRepository(session)

    def claim_due_schedules(
        self, now: datetime | None = None, *, limit: int = 50, skip_locked: bool = True
    ) -> list[ProjectScanSchedule]:
        """Claim due schedules using FOR UPDATE SKIP LOCKED.

        Multiple scheduler workers are safe — two workers cannot claim
        the same schedule row.
        """
        if now is None:
            now = datetime.now(UTC)

        rows = (
            self._session.execute(
                select(ProjectScanSchedule)
                .where(
                    ProjectScanSchedule.enabled.is_(True),
                    ProjectScanSchedule.next_run_at <= now,
                )
                .order_by(ProjectScanSchedule.next_run_at)
                .limit(limit)
                .with_for_update(skip_locked=skip_locked)
            )
            .scalars()
            .all()
        )
        return list(rows)

    def evaluate_due_schedule(
        self, schedule: ProjectScanSchedule, *, now: datetime | None = None
    ) -> SchedulerEvaluationResult:
        """Evaluate one due schedule and trigger a scan if appropriate.

        At most ONE scan is created per evaluation. next_run_at is
        advanced to the first future interval boundary regardless of
        outcome (no catch-up storm).
        """
        if now is None:
            now = datetime.now(UTC)

        scheduled_for = schedule.next_run_at
        schedule.last_due_at = scheduled_for

        # 1. Recheck current entitlement (can change after creation).
        ent = self._entitlements.get_effective_entitlements(schedule.workspace_id)
        min_interval = ent.min_scheduled_scan_interval_hours

        if min_interval is None:
            return self._skip(
                schedule,
                ScheduledScanOutcome.SKIPPED_ENTITLEMENT,
                "Scheduled scans not available on current plan.",
                now,
            )

        if schedule.interval_hours < min_interval:
            return self._skip(
                schedule,
                ScheduledScanOutcome.SKIPPED_ENTITLEMENT,
                f"Interval {schedule.interval_hours}h is below plan minimum {min_interval}h.",
                now,
            )

        # 2. Validate project is active.
        project = self._session.get(Project, schedule.project_id)
        if project is None or project.status != ProjectStatus.ACTIVE:
            return self._skip(
                schedule,
                ScheduledScanOutcome.SKIPPED_PROJECT_INACTIVE,
                "Project is not active.",
                now,
            )

        # 3. Check for conflicting active scheduled scan.
        active = self._find_active_scheduled_scan(schedule.workspace_id, schedule.project_id)
        if active is not None:
            return self._skip(
                schedule,
                ScheduledScanOutcome.SKIPPED_ACTIVE_SCAN,
                f"Active scheduled scan {active.id} already in progress.",
                now,
            )

        # 4. Create STANDARD scan via ScanCreationService.
        # Capture IDs before the call — ScanCreationService may rollback
        # the session on error, invalidating the schedule ORM object.
        schedule_id = schedule.id
        workspace_id = schedule.workspace_id
        project_id = schedule.project_id
        created_by_user_id = schedule.created_by_user_id

        idempotency_key = self._slot_idempotency_key(schedule_id, scheduled_for)

        creation_service = ScanCreationService(
            self._session,
            self._dispatcher,
            audit_service=self._audit,
        )

        sp = self._session.begin_nested()
        try:
            result = creation_service.create_scan(
                workspace_id=workspace_id,
                project_id=project_id,
                scan_type=ScanType.STANDARD,
                requested_by_user_id=created_by_user_id,
                idempotency_key=idempotency_key,
                scan_schedule_id=schedule_id,
                scheduled_for=scheduled_for,
            )
            sp.commit()
        except QuotaExceededError:
            with contextlib.suppress(Exception):
                sp.rollback()
            return self._skip_reloaded(
                schedule_id,
                ScheduledScanOutcome.SKIPPED_QUOTA,
                "Insufficient monthly AI checks quota.",
                now,
            )
        except (ValidationError, ConflictError) as exc:
            with contextlib.suppress(Exception):
                sp.rollback()
            return self._skip_reloaded(
                schedule_id,
                ScheduledScanOutcome.SKIPPED_NOT_READY,
                f"Project not ready: {exc}",
                now,
            )
        except NotFoundError:
            with contextlib.suppress(Exception):
                sp.rollback()
            return self._skip_reloaded(
                schedule_id,
                ScheduledScanOutcome.SKIPPED_NOT_READY,
                "Project not found.",
                now,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                sp.rollback()
            logger.error(
                "schedule_scan_creation_failed",
                schedule_id=str(schedule_id),
                error=str(exc),
            )
            return self._skip_reloaded(
                schedule_id,
                ScheduledScanOutcome.DISPATCH_FAILED,
                f"Scan creation failed: {type(exc).__name__}",
                now,
            )

        # 5. Record success.
        schedule.last_triggered_at = now
        schedule.last_scan_id = result.scan.id
        schedule.last_outcome = ScheduledScanOutcome.TRIGGERED
        schedule.last_skip_reason = None
        self._advance_next_run(schedule, now)
        self._session.commit()

        self._record_audit("SCHEDULE_SCAN_TRIGGERED", schedule)

        return SchedulerEvaluationResult(
            schedule_id=schedule.id,
            outcome=ScheduledScanOutcome.TRIGGERED,
            scan_id=result.scan.id,
        )

    def _skip(
        self,
        schedule: ProjectScanSchedule,
        outcome: ScheduledScanOutcome,
        reason: str,
        now: datetime,
    ) -> SchedulerEvaluationResult:
        """Record a skip outcome and advance next_run_at."""
        schedule.last_outcome = outcome
        schedule.last_skip_reason = reason
        self._advance_next_run(schedule, now)
        self._session.commit()

        self._record_audit("SCHEDULE_SCAN_SKIPPED", schedule)

        return SchedulerEvaluationResult(
            schedule_id=schedule.id,
            outcome=outcome,
            skip_reason=reason,
        )

    def _skip_reloaded(
        self,
        schedule_id: uuid.UUID,
        outcome: ScheduledScanOutcome,
        reason: str,
        now: datetime,
    ) -> SchedulerEvaluationResult:
        """Reload the schedule after a rollback, then skip.

        Used when ScanCreationService rolled back the session on error,
        invalidating the original schedule ORM object.
        """
        schedule = (
            self._session.execute(
                select(ProjectScanSchedule).where(ProjectScanSchedule.id == schedule_id)
            )
            .scalars()
            .first()
        )
        if schedule is None:
            return SchedulerEvaluationResult(
                schedule_id=schedule_id,
                outcome=outcome,
                skip_reason=reason,
            )
        return self._skip(schedule, outcome, reason, now)

    def _advance_next_run(self, schedule: ProjectScanSchedule, now: datetime) -> None:
        """Advance next_run_at to the first future interval boundary.

        No catch-up storm: if multiple intervals have passed, we skip
        directly to the next future slot.
        """
        interval = timedelta(hours=schedule.interval_hours)
        next_run = schedule.next_run_at
        # Advance until next_run is in the future.
        while next_run <= now:
            next_run = next_run + interval
        schedule.next_run_at = next_run

    def _find_active_scheduled_scan(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> Scan | None:
        """Find a PENDING or RUNNING scheduled scan for this project."""
        return (
            self._session.execute(
                select(Scan).where(
                    Scan.workspace_id == workspace_id,
                    Scan.project_id == project_id,
                    Scan.scan_schedule_id.is_not(None),
                    Scan.status.in_([ScanStatus.PENDING, ScanStatus.RUNNING]),
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    def _slot_idempotency_key(schedule_id: uuid.UUID, scheduled_for: datetime) -> str:
        """Deterministic idempotency key for one due slot.

        Format: scheduled:{schedule_uuid}:{scheduled_for_iso}
        """
        return f"scheduled:{schedule_id}:{scheduled_for.isoformat()}"

    def _record_audit(self, event_type: str, schedule: ProjectScanSchedule) -> None:
        if self._audit is None:
            return
        self._audit.record(
            action=event_type,
            workspace_id=schedule.workspace_id,
            entity_type="schedule",
            entity_id=schedule.id,
        )

    def create_or_update_schedule(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        enabled: bool,
        interval_hours: int,
        created_by_user_id: uuid.UUID,
        first_run_at: datetime | None = None,
    ) -> ProjectScanSchedule:
        """Create or replace a project scan schedule.

        Validates entitlement at creation/update time.
        """
        ent = self._entitlements.get_effective_entitlements(workspace_id)
        min_interval = ent.min_scheduled_scan_interval_hours

        if min_interval is None:
            raise ValidationError("Scheduled scans are not available on your plan.")
        if interval_hours < min_interval:
            raise ValidationError(
                f"Interval {interval_hours}h is below the minimum {min_interval}h for your plan."
            )
        if interval_hours <= 0:
            raise ValidationError("Interval hours must be positive.")

        # Check for existing schedule (one per project).
        existing = (
            self._session.execute(
                select(ProjectScanSchedule)
                .where(ProjectScanSchedule.project_id == project_id)
                .with_for_update()
            )
            .scalars()
            .first()
        )

        now = datetime.now(UTC)
        next_run = (
            first_run_at if first_run_at is not None else now + timedelta(hours=interval_hours)
        )

        if existing is not None:
            existing.enabled = enabled
            existing.interval_hours = interval_hours
            if (enabled and existing.next_run_at <= now) or first_run_at is not None:
                existing.next_run_at = next_run
            self._session.commit()
            self._record_audit("SCHEDULE_UPDATED", existing)
            return existing

        schedule = ProjectScanSchedule(
            workspace_id=workspace_id,
            project_id=project_id,
            enabled=enabled,
            interval_hours=interval_hours,
            next_run_at=next_run,
            created_by_user_id=created_by_user_id,
        )
        self._session.add(schedule)
        self._session.commit()
        self._record_audit("SCHEDULE_CREATED", schedule)
        return schedule

    def get_schedule(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> ProjectScanSchedule | None:
        """Get the schedule for a project, or None."""
        return (
            self._session.execute(
                select(ProjectScanSchedule).where(
                    ProjectScanSchedule.workspace_id == workspace_id,
                    ProjectScanSchedule.project_id == project_id,
                )
            )
            .scalars()
            .first()
        )

    def disable_schedule(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> ProjectScanSchedule | None:
        """Disable a schedule (does not delete)."""
        schedule = self.get_schedule(workspace_id, project_id)
        if schedule is None:
            return None
        schedule.enabled = False
        self._session.commit()
        self._record_audit("SCHEDULE_DISABLED", schedule)
        return schedule
