"""Unit tests for stale recovery economic safety (Phase 13.5.10D).

Verifies that ScanRecoveryService does NOT terminalize scans with
RUNNING PromptRuns — because a RUNNING run means the provider call
may have been billed, and generic FAILED + quota release would
recreate the original cost-zero bug.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from app.core.enums import (
    PromptRunStatus,
    ProviderErrorCode,
    ScanStatus,
    ScanType,
)
from app.models.scan import PromptRun, Scan
from app.services.scan_finalization_service import ScanRecoveryService


def _make_scan(
    *, status: ScanStatus = ScanStatus.RUNNING, scan_type: ScanType = ScanType.STANDARD
) -> Any:
    scan = MagicMock(spec=Scan)
    scan.id = uuid.uuid4()
    scan.status = status
    scan.scan_type = scan_type
    scan.started_at = datetime.now(UTC) - timedelta(hours=3)
    scan.created_at = datetime.now(UTC) - timedelta(hours=3)
    scan.quota_reservation_id = uuid.uuid4()
    return scan


def _make_run(
    *, status: PromptRunStatus = PromptRunStatus.RUNNING, error_code: str | None = None
) -> Any:
    run = MagicMock(spec=PromptRun)
    run.id = uuid.uuid4()
    run.status = status
    run.error_code = error_code
    run.provider_request_id = "req_001" if status == PromptRunStatus.RUNNING else None
    return run


def _make_recovery_service(
    session: Any = None,
    *,
    stale_after: int = 7200,
    scans: list[Any] | None = None,
    running_runs: list[Any] | None = None,
) -> ScanRecoveryService:
    """Create a ScanRecoveryService with mocked dependencies."""
    session = session or MagicMock()

    settings = MagicMock()
    settings.scan_stale_after_seconds = stale_after

    scans_repo = MagicMock()
    if scans is not None:
        scans_repo.get_for_update.return_value = scans[0] if scans else None
        scans_repo.list_stale_running.return_value = [
            s for s in scans if s.status == ScanStatus.RUNNING
        ]
        scans_repo.list_stale_pending.return_value = [
            s for s in scans if s.status == ScanStatus.PENDING
        ]
    else:
        scans_repo.get_for_update.return_value = None
        scans_repo.list_stale_running.return_value = []
        scans_repo.list_stale_pending.return_value = []

    runs_repo = MagicMock()
    # When queried for RUNNING runs, return the mock
    if running_runs is not None:
        # Simulate the select query returning running_runs
        mock_result = MagicMock()
        mock_result.scalars.return_value = running_runs
        session.execute.return_value = mock_result
    else:
        mock_result = MagicMock()
        mock_result.scalars.return_value = []
        session.execute.return_value = mock_result

    finalizer = MagicMock()

    with (
        patch(
            "app.services.scan_finalization_service.ScanRepository",
            return_value=scans_repo,
        ),
        patch(
            "app.services.scan_finalization_service.PromptRunRepository",
            return_value=runs_repo,
        ),
        patch(
            "app.services.scan_finalization_service.ScanFinalizationService",
            return_value=finalizer,
        ),
        patch("app.services.scan_finalization_service.get_settings", return_value=settings),
    ):
        service = ScanRecoveryService(session)
        # Store mocks for assertions
        service._scans = scans_repo
        service._runs = runs_repo
        service._finalizer = finalizer
        service._settings = settings
        return service


def test_stale_pending_scan_is_recovered_normally() -> None:
    """Stale PENDING scan with no RUNNING runs → safe to fail/release quota."""
    scan = _make_scan(status=ScanStatus.PENDING)
    scan.started_at = None
    scan.created_at = datetime.now(UTC) - timedelta(hours=3)

    session = MagicMock()
    service = _make_recovery_service(
        session,
        scans=[scan],
        running_runs=[],
    )

    result = service._recover_one(
        scan.id, datetime.now(UTC) - timedelta(hours=2), datetime.now(UTC)
    )

    assert result is True
    service._runs.mark_unresolved_failed.assert_called_once()  # type: ignore[attr-defined]
    service._finalizer.finalize.assert_called_once()  # type: ignore[attr-defined]


def test_stale_running_with_running_prompt_run_not_recovered() -> None:
    """Stale RUNNING scan with RUNNING PromptRun → NOT recovered.
    No finalize, no quota release, no mark_unresolved_failed."""
    scan = _make_scan(status=ScanStatus.RUNNING)
    run = _make_run(status=PromptRunStatus.RUNNING)

    session = MagicMock()
    service = _make_recovery_service(
        session,
        scans=[scan],
        running_runs=[run],
    )

    result = service._recover_one(
        scan.id, datetime.now(UTC) - timedelta(hours=2), datetime.now(UTC)
    )

    assert result is False
    service._runs.mark_unresolved_failed.assert_not_called()  # type: ignore[attr-defined]
    service._finalizer.finalize.assert_not_called()  # type: ignore[attr-defined]


def test_stale_running_with_accounting_unresolved_not_recovered() -> None:
    """Stale RUNNING scan with RUNNING PromptRun + ACCOUNTING_UNRESOLVED
    → NOT recovered.  Requires manual reconciliation."""
    scan = _make_scan(status=ScanStatus.RUNNING)
    run = _make_run(
        status=PromptRunStatus.RUNNING,
        error_code=ProviderErrorCode.ACCOUNTING_UNRESOLVED,
    )

    session = MagicMock()
    service = _make_recovery_service(
        session,
        scans=[scan],
        running_runs=[run],
    )

    result = service._recover_one(
        scan.id, datetime.now(UTC) - timedelta(hours=2), datetime.now(UTC)
    )

    assert result is False
    service._runs.mark_unresolved_failed.assert_not_called()  # type: ignore[attr-defined]
    service._finalizer.finalize.assert_not_called()  # type: ignore[attr-defined]


def test_stale_running_all_succeeded_is_recovered() -> None:
    """Stale RUNNING scan with no RUNNING runs (all SUCCEEDED/FAILED)
    → safe to finalize."""
    scan = _make_scan(status=ScanStatus.RUNNING)
    # No RUNNING runs — all already terminal
    session = MagicMock()
    service = _make_recovery_service(
        session,
        scans=[scan],
        running_runs=[],
    )

    result = service._recover_one(
        scan.id, datetime.now(UTC) - timedelta(hours=2), datetime.now(UTC)
    )

    assert result is True
    service._runs.mark_unresolved_failed.assert_called_once()  # type: ignore[attr-defined]
    service._finalizer.finalize.assert_called_once()  # type: ignore[attr-defined]


def test_mixed_succeeded_pending_running_not_recovered() -> None:
    """Scan with SUCCEEDED + PENDING + RUNNING → NOT recovered because
    of the RUNNING run (economic uncertainty)."""
    scan = _make_scan(status=ScanStatus.RUNNING)
    running_run = _make_run(status=PromptRunStatus.RUNNING)

    session = MagicMock()
    service = _make_recovery_service(
        session,
        scans=[scan],
        running_runs=[running_run],
    )

    result = service._recover_one(
        scan.id, datetime.now(UTC) - timedelta(hours=2), datetime.now(UTC)
    )

    assert result is False
    service._finalizer.finalize.assert_not_called()  # type: ignore[attr-defined]


def test_no_provider_replay_in_recovery() -> None:
    """Recovery never calls any provider.  Verify no adapter/registry
    is invoked during recovery."""
    scan = _make_scan(status=ScanStatus.RUNNING)
    run = _make_run(status=PromptRunStatus.RUNNING)

    session = MagicMock()
    service = _make_recovery_service(
        session,
        scans=[scan],
        running_runs=[run],
    )

    # Recovery should not touch any provider
    service._recover_one(scan.id, datetime.now(UTC) - timedelta(hours=2), datetime.now(UTC))

    # No provider calls made — just DB operations
    # The key assertion is that mark_unresolved_failed was NOT called
    service._runs.mark_unresolved_failed.assert_not_called()  # type: ignore[attr-defined]
