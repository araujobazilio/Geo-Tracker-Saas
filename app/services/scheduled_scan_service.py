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

Concurrency architecture (Phase 11.1):
- The scheduler session (Session A) holds the FOR UPDATE lock on the
  ProjectScanSchedule row for the entire evaluation.
- Scan creation runs in an INDEPENDENT session (Session B) via
  scan_creation_session_factory. Session B may commit freely without
  releasing the schedule row lock.
- After scan creation returns, Session A updates last_outcome /
  next_run_at and commits — only then is the row lock released.
- process_due_schedules processes ONE schedule at a time: BEGIN ->
  claim ONE -> evaluate -> commit -> repeat. This avoids holding a batch
  of 50 locked rows whose locks are released by the first commit.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
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
from app.models.project_scan_schedule import ProjectScanSchedule
from app.models.scan import Scan
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import ScanRepository
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService
from app.services.scan_creation_service import ScanCreationResult, ScanCreationService
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
    execution continues via scan.execute Celery tasks.
    """

    def __init__(
        self,
        session: Session,
        dispatcher: ScanDispatcher,
        *,
        audit_service: AuditService | None = None,
        scan_creation_session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._session = session
        self._dispatcher = dispatcher
        self._audit = audit_service
        self._scan_creation_session_factory = scan_creation_session_factory
        self._entitlements = EntitlementService(session)
        self._scans = ScanRepository(session)
        self._projects = ProjectRepository(session)

    def process_due_schedules(
        self,
        now: datetime | None = None,
        *,
        limit: int = 50,
    ) -> dict[str, int]:
        """Process due schedules ONE AT A TIME.

        For each due schedule:
        1. BEGIN (outer transaction already active).
        2. Claim ONE due schedule with FOR UPDATE SKIP LOCKED.
        3. Evaluate that due slot (scan creation uses independent session).
        4. Commit schedule decision (releases row lock).
        5. Repeat up to limit times.

        Returns a dict of outcome -> count.
        """
        if now is None:
            now = datetime.now(UTC)

        results: dict[str, int] = {}
        for _ in range(limit):
            schedule = self._claim_one_due_schedule(now)
            if schedule is None:
                break

            result = self.evaluate_due_schedule(schedule, now=now)
            outcome = result.outcome.value
            results[outcome] = results.get(outcome, 0) + 1

        return results

    def _claim_one_due_schedule(self, now: datetime) -> ProjectScanSchedule | None:
        """Claim a single due schedule with FOR UPDATE SKIP LOCKED."""
        rows = (
            self._session.execute(
                select(ProjectScanSchedule)
                .where(
                    ProjectScanSchedule.enabled.is_(True),
                    ProjectScanSchedule.next_run_at <= now,
                )
                .order_by(ProjectScanSchedule.next_run_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        return rows[0] if rows else None

    def claim_due_schedules(
        self, now: datetime | None = None, *, limit: int = 50, skip_locked: bool = True
    ) -> list[ProjectScanSchedule]:
        """Claim due schedules using FOR UPDATE SKIP LOCKED.

        Prefer process_due_schedules for production use. This method is
        kept for testing and backward compatibility.
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

        The schedule row lock (held on self._session) is preserved
        throughout. Scan creation runs in an INDEPENDENT session so its
        commits do not release the schedule lock.
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

        # 2. Validate project is active (tenant-scoped).
        project = self._projects.get_in_workspace(schedule.project_id, schedule.workspace_id)
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
        # Use an INDEPENDENT session so ScanCreationService commits do
        # NOT release the schedule row lock held on self._session.
        schedule_id = schedule.id
        workspace_id = schedule.workspace_id
        project_id = schedule.project_id
        created_by_user_id = schedule.created_by_user_id

        idempotency_key = self._slot_idempotency_key(schedule_id, scheduled_for)

        try:
            result = self._create_scan_in_independent_session(
                workspace_id=workspace_id,
                project_id=project_id,
                created_by_user_id=created_by_user_id,
                idempotency_key=idempotency_key,
                schedule_id=schedule_id,
                scheduled_for=scheduled_for,
            )
        except QuotaExceededError:
            return self._skip(
                schedule,
                ScheduledScanOutcome.SKIPPED_QUOTA,
                "Insufficient monthly AI checks quota.",
                now,
            )
        except (ValidationError, ConflictError) as exc:
            return self._skip(
                schedule,
                ScheduledScanOutcome.SKIPPED_NOT_READY,
                f"Project not ready: {exc}",
                now,
            )
        except NotFoundError:
            return self._skip(
                schedule,
                ScheduledScanOutcome.SKIPPED_NOT_READY,
                "Project not found.",
                now,
            )
        except Exception as exc:
            logger.error(
                "schedule_scan_creation_failed",
                schedule_id=str(schedule_id),
                error=str(exc),
            )
            return self._skip(
                schedule,
                ScheduledScanOutcome.DISPATCH_FAILED,
                f"Scan creation failed: {type(exc).__name__}",
                now,
            )

        # 5. Record success — schedule row lock is still held.
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

    def _create_scan_in_independent_session(
        self,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        created_by_user_id: uuid.UUID,
        idempotency_key: str,
        schedule_id: uuid.UUID,
        scheduled_for: datetime,
    ) -> ScanCreationResult:
        """Create a scan in an independent session.

        If scan_creation_session_factory was provided, use it.
        Otherwise, fall back to using the same session (for tests that
        do not need the concurrency guarantee).
        """
        if self._scan_creation_session_factory is not None:
            scan_session = self._scan_creation_session_factory()
            try:
                creation_service = ScanCreationService(
                    scan_session,
                    self._dispatcher,
                    audit_service=self._audit,
                )
                result = creation_service.create_scan(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    scan_type=ScanType.STANDARD,
                    requested_by_user_id=created_by_user_id,
                    idempotency_key=idempotency_key,
                    scan_schedule_id=schedule_id,
                    scheduled_for=scheduled_for,
                )
                scan_session.commit()
                return result
            except Exception:
                with contextlib.suppress(Exception):
                    scan_session.rollback()
                raise
            finally:
                scan_session.close()
        else:
            creation_service = ScanCreationService(
                self._session,
                self._dispatcher,
                audit_service=self._audit,
            )
            return creation_service.create_scan(
                workspace_id=workspace_id,
                project_id=project_id,
                scan_type=ScanType.STANDARD,
                requested_by_user_id=created_by_user_id,
                idempotency_key=idempotency_key,
                scan_schedule_id=schedule_id,
                scheduled_for=scheduled_for,
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

    def _advance_next_run(self, schedule: ProjectScanSchedule, now: datetime) -> None:
        """Advance next_run_at to the first future interval boundary.

        No catch-up storm: if multiple intervals have passed, we skip
        directly to the next future slot.
        """
        interval = timedelta(hours=schedule.interval_hours)
        next_run = schedule.next_run_at
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
        Resolves Project with BOTH workspace_id and project_id to
        enforce tenant isolation.
        """
        # Tenant isolation: resolve project with workspace_id + project_id.
        project = self._projects.get_in_workspace(project_id, workspace_id)
        if project is None:
            raise NotFoundError("Project not found in this workspace.")

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

        # Normalize first_run_at to timezone-aware UTC.
        next_run: datetime
        if first_run_at is not None:
            next_run = self._normalize_to_utc(first_run_at)
        else:
            next_run = datetime.now(UTC) + timedelta(hours=interval_hours)

        # Check for existing schedule (tenant-scoped: workspace_id + project_id).
        existing = (
            self._session.execute(
                select(ProjectScanSchedule)
                .where(
                    ProjectScanSchedule.workspace_id == workspace_id,
                    ProjectScanSchedule.project_id == project_id,
                )
                .with_for_update()
            )
            .scalars()
            .first()
        )

        now = datetime.now(UTC)

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

    @staticmethod
    def _normalize_to_utc(dt: datetime) -> datetime:
        """Normalize a datetime to timezone-aware UTC.

        Naive datetimes are assumed to be UTC and explicitly tagged.
        Timezone-aware datetimes are converted to UTC.
        """
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    def get_schedule(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> ProjectScanSchedule | None:
        """Get the schedule for a project, or None. Tenant-scoped."""
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
        """Disable a schedule (does not delete). Tenant-scoped."""
        schedule = self.get_schedule(workspace_id, project_id)
        if schedule is None:
            return None
        schedule.enabled = False
        self._session.commit()
        self._record_audit("SCHEDULE_DISABLED", schedule)
        return schedule
