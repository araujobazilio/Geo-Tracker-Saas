"""Scheduled scan notification generation.

After a SCHEDULED STANDARD Scan reaches terminal status and deterministic
ScanAnalysis is COMPLETED, generate:
1. Scheduled scan summary notification (COMPLETED/PARTIAL/FAILED).
2. Automatic Action Center refresh (local, deterministic, 0 provider calls).
3. High-priority opportunity alerts (deduplicated per Opportunity.id).

This service does NOT consume AI Checks or call LLM providers.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    NotificationType,
    OpportunityPriority,
    OpportunityStatus,
    ScanAnalysisStatus,
    ScanStatus,
    ScanType,
)
from app.core.logging import get_logger
from app.models.analysis import ScanAnalysis
from app.models.opportunity import Opportunity
from app.models.scan import Scan
from app.services.action_generation_service import ActionGenerationService
from app.services.email_templates import build_scheduled_scan_message
from app.services.notification_service import NotificationInput, NotificationService

logger = get_logger("app.scheduled_scan_notifications")


class ScheduledScanNotificationService:
    """Generate notifications for scheduled scans after finalization.

    This service is called AFTER scan finalization + analysis completion.
    It is safe to call multiple times — notifications are deduplicated.
    """

    def __init__(
        self,
        session: Session,
        notification_service: NotificationService | None = None,
    ) -> None:
        self._session = session
        self._notification_service = notification_service or NotificationService(session)

    def handle_scheduled_scan_terminal(self, scan_id: uuid.UUID) -> None:
        """Handle a terminal scheduled scan: refresh actions + notifications.

        This is the main entry point. It:
        1. Loads the scan and verifies it is scheduled + terminal.
        2. Runs automatic Action Center refresh if analysis is COMPLETED.
        3. Generates scan summary notification.
        4. Generates high-priority opportunity notifications.

        All operations are idempotent (deduplicated).
        """
        scan = self._session.get(Scan, scan_id)
        if scan is None:
            return
        if scan.scan_schedule_id is None:
            return  # Not a scheduled scan.
        if scan.scan_type != ScanType.STANDARD:
            return
        if scan.status not in (ScanStatus.COMPLETED, ScanStatus.PARTIAL, ScanStatus.FAILED):
            return

        # Run automatic Action Center refresh for COMPLETED/PARTIAL with analysis.
        open_opportunities_count: int | None = None
        if scan.status in (ScanStatus.COMPLETED, ScanStatus.PARTIAL):
            analysis = (
                self._session.execute(select(ScanAnalysis).where(ScanAnalysis.scan_id == scan_id))
                .scalars()
                .first()
            )

            if analysis is not None and analysis.status == ScanAnalysisStatus.COMPLETED:
                open_opportunities_count = self._auto_refresh_actions(scan)
            elif analysis is not None and analysis.status == ScanAnalysisStatus.FAILED:
                # Analysis failed — still notify but without opportunity count.
                pass

        # Generate scan summary notification.
        self._generate_scan_summary_notification(scan, open_opportunities_count)

        # Generate high-priority opportunity notifications.
        if open_opportunities_count is not None:
            self._generate_high_priority_notifications(scan)

    def _auto_refresh_actions(self, scan: Scan) -> int | None:
        """Run ActionGenerationService.refresh_from_scan for scheduled scans.

        Returns the count of open opportunities, or None if refresh failed.
        Failure does NOT rollback the scan or analysis.
        """
        try:
            service = ActionGenerationService(self._session)
            service.refresh_from_scan(
                workspace_id=scan.workspace_id,
                project_id=scan.project_id,
                scan_id=scan.id,
            )
            self._session.commit()
            # Count open opportunities for this project.
            count = (
                self._session.execute(
                    select(Opportunity).where(
                        Opportunity.workspace_id == scan.workspace_id,
                        Opportunity.project_id == scan.project_id,
                        Opportunity.status == OpportunityStatus.OPEN,
                    )
                )
                .scalars()
                .all()
            )
            return len(count)
        except Exception as exc:
            logger.error(
                "auto_action_refresh_failed",
                scan_id=str(scan.id),
                error=str(exc),
            )
            self._session.rollback()
            return None

    def _generate_scan_summary_notification(
        self, scan: Scan, open_opportunities_count: int | None
    ) -> None:
        """Generate the scheduled scan summary notification."""
        if scan.status == ScanStatus.COMPLETED:
            ntype = NotificationType.SCHEDULED_SCAN_COMPLETED
        elif scan.status == ScanStatus.PARTIAL:
            ntype = NotificationType.SCHEDULED_SCAN_PARTIAL
        else:
            ntype = NotificationType.SCHEDULED_SCAN_FAILED

        message = build_scheduled_scan_message(
            scan,
            open_opportunities=open_opportunities_count,
        )

        dedup_key = f"scheduled-scan:{scan.id}:terminal"
        deep_link = f"/projects/{scan.project_id}/scans/{scan.id}"

        self._notify_workspace_members(
            workspace_id=scan.workspace_id,
            notification_type=ntype,
            title=f"Scheduled scan {scan.status.value.lower()}",
            message=message,
            dedup_key=dedup_key,
            project_id=scan.project_id,
            scan_id=scan.id,
            deep_link_path=deep_link,
        )

    def _generate_high_priority_notifications(self, scan: Scan) -> None:
        """Generate NEW_HIGH_PRIORITY_OPPORTUNITY notifications.

        Only for opportunities newly created by this scan (deduplicated
        per Opportunity.id, not per Scan ID).
        """
        # Find HIGH priority opportunities for this project.
        opportunities = (
            self._session.execute(
                select(Opportunity).where(
                    Opportunity.workspace_id == scan.workspace_id,
                    Opportunity.project_id == scan.project_id,
                    Opportunity.priority == OpportunityPriority.HIGH,
                )
            )
            .scalars()
            .all()
        )

        for opp in opportunities:
            dedup_key = f"opportunity:{opp.id}:first-high"
            deep_link = f"/projects/{scan.project_id}/opportunities/{opp.id}"

            self._notify_workspace_members(
                workspace_id=scan.workspace_id,
                notification_type=NotificationType.NEW_HIGH_PRIORITY_OPPORTUNITY,
                title="New high-priority opportunity detected",
                message=f"Opportunity: {opp.title}",
                dedup_key=dedup_key,
                project_id=scan.project_id,
                opportunity_id=opp.id,
                deep_link_path=deep_link,
            )

    def _notify_workspace_members(
        self,
        workspace_id: uuid.UUID,
        notification_type: NotificationType,
        title: str,
        message: str,
        dedup_key: str,
        project_id: uuid.UUID | None = None,
        scan_id: uuid.UUID | None = None,
        opportunity_id: uuid.UUID | None = None,
        verification_id: uuid.UUID | None = None,
        deep_link_path: str | None = None,
    ) -> None:
        """Create a notification for all active workspace members.

        Deduplication is per-user (user_id + dedup_key unique).
        """
        recipients = self._notification_service.list_active_recipients(workspace_id)
        for _member, user in recipients:
            inp = NotificationInput(
                workspace_id=workspace_id,
                user_id=user.id,
                notification_type=notification_type,
                title=title,
                message=message,
                dedup_key=dedup_key,
                project_id=project_id,
                scan_id=scan_id,
                opportunity_id=opportunity_id,
                verification_id=verification_id,
                deep_link_path=deep_link_path,
            )
            try:
                self._notification_service.create_notification(inp)
                self._session.commit()
            except Exception as exc:
                logger.warning(
                    "notification_creation_failed",
                    user_id=str(user.id),
                    dedup_key=dedup_key,
                    error=str(exc),
                )
                self._session.rollback()
