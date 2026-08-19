"""Economically safe Scan task: no automatic paid-call retries."""

from __future__ import annotations

import asyncio
import uuid

from app.db.session import get_session_factory
from app.services.audit_service import AuditService
from app.services.scan_execution_service import ScanExecutionService
from app.workers.celery_app import celery_app


@celery_app.task(name="scan.execute")
def execute_scan_task(scan_id: str) -> None:
    scan_uuid = uuid.UUID(scan_id)
    factory = get_session_factory()
    service = ScanExecutionService(factory, audit_service=AuditService(factory))
    asyncio.run(service.execute_scan(scan_uuid))
