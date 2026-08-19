"""Scan dispatch abstraction; PostgreSQL remains product-state authority."""

from __future__ import annotations

import uuid
from typing import Protocol


class ScanDispatcher(Protocol):
    def dispatch(self, scan_id: uuid.UUID) -> None: ...


class CeleryScanDispatcher:
    def dispatch(self, scan_id: uuid.UUID) -> None:
        from app.workers.scan_tasks import execute_scan_task

        execute_scan_task.delay(str(scan_id))
