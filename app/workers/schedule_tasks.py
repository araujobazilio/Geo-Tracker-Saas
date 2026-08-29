"""Celery tasks for scheduled scan dispatch.

The Beat task ``schedule.dispatch_due`` runs periodically (every 60s by
default). It claims due schedules from PostgreSQL and creates/dispatches
STANDARD scans. It does NOT execute provider requests itself — normal
``scan.execute`` continues doing provider execution.

Concurrency architecture (Phase 11.1):
- ``process_due_schedules`` processes ONE schedule at a time.
- Each schedule is claimed with FOR UPDATE SKIP LOCKED.
- Scan creation runs in an independent session so its commits do not
  release the schedule row lock.
- The schedule row lock is held until next_run_at is advanced and
  committed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.session import get_session_factory
from app.services.audit_service import AuditService
from app.services.scanning.dispatcher import CeleryScanDispatcher
from app.services.scheduled_scan_service import ScheduledScanService
from app.workers.celery_app import celery_app


@celery_app.task(name="schedule.dispatch_due")
def dispatch_due_schedules_task() -> dict[str, int]:
    """Claim and evaluate all due schedules.

    Uses ``process_due_schedules`` which processes ONE schedule at a
    time with proper row-lock lifetime management.

    Returns a summary of outcomes for observability.
    """
    factory = get_session_factory()
    audit = AuditService(factory)

    with factory() as session:
        service = ScheduledScanService(
            session,
            dispatcher=CeleryScanDispatcher(),
            audit_service=audit,
            scan_creation_session_factory=factory,
        )
        now = datetime.now(UTC)
        results = service.process_due_schedules(now=now)

    return results
