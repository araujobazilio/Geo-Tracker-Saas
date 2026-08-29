"""Celery tasks for scheduled scan dispatch.

The Beat task ``schedule.dispatch_due`` runs periodically (every 60s by
default). It claims due schedules from PostgreSQL and creates/dispatches
STANDARD scans. It does NOT execute provider requests itself — normal
``scan.execute`` continues doing provider execution.
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

    Returns a summary of outcomes for observability.
    """
    factory = get_session_factory()
    audit = AuditService(factory)
    results: dict[str, int] = {}

    with factory() as session:
        service = ScheduledScanService(
            session,
            dispatcher=CeleryScanDispatcher(),
            audit_service=audit,
        )
        now = datetime.now(UTC)
        due = service.claim_due_schedules(now=now)

        for schedule in due:
            result = service.evaluate_due_schedule(schedule, now=now)
            outcome = result.outcome.value
            results[outcome] = results.get(outcome, 0) + 1

    return results
