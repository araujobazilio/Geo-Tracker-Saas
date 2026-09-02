"""DashboardQueryService — read-only orchestration for web dashboard rendering.

This service composes data from existing services and repositories but
does NOT recreate any metric formulas. It is strictly READ-ONLY — no
write operations, no provider calls, no AI Check consumption.

Responsibilities:
  - Latest eligible STANDARD Scan for a project
  - Recent eligible STANDARD Scans (for trend charts)
  - Latest metrics (via VisibilityMetricsService)
  - Project summary cards
  - Workspace counters
  - Recent Scan list
  - Usage/quota summary
  - Next schedule info
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import (
    OpportunityPriority,
    OpportunityStatus,
    ProjectStatus,
    PromptType,
    ScanAnalysisStatus,
    ScanStatus,
    ScanType,
)
from app.models.analysis import ScanAnalysis
from app.models.notification import Notification
from app.models.opportunity import Opportunity
from app.models.project import Project
from app.models.project_scan_schedule import ProjectScanSchedule
from app.models.scan import Scan
from app.services.entitlement_service import EntitlementService
from app.services.visibility_metrics_service import MetricsResult, VisibilityMetricsService
from app.web.context import QuotaSummary


@dataclass(frozen=True)
class ProjectCard:
    """Summary data for a project card on the workspace dashboard."""

    id: uuid.UUID
    name: str
    domain: str
    brand_name: str
    status: ProjectStatus
    last_scan_at: datetime | None
    latest_scan_status: ScanStatus | None
    latest_scan_id: uuid.UUID | None
    visibility_rate: Decimal | None
    measurement_coverage: Decimal | None
    high_open_opportunities: int
    next_scheduled_run: datetime | None
    is_prompt_set_stale: bool


@dataclass(frozen=True)
class WorkspaceOverview:
    """Aggregated data for the workspace overview dashboard."""

    active_projects: int
    total_projects: int
    quota: QuotaSummary
    high_open_opportunities: int
    unread_notifications: int
    next_scheduled_run: datetime | None
    projects: list[ProjectCard] = field(default_factory=list)
    plan_name: str = ""


@dataclass(frozen=True)
class ScanHistoryItem:
    """One row in the recent scans list."""

    id: uuid.UUID
    scan_type: ScanType
    status: ScanStatus
    created_at: datetime
    completed_at: datetime | None
    successful_observations: int
    failed_observations: int
    planned_observations: int
    coverage: Decimal | None
    origin: str  # "Manual", "Scheduled", "Verification", "Confidence"


@dataclass(frozen=True)
class TrendPoint:
    """One data point in a visibility trend chart."""

    scan_id: uuid.UUID
    created_at: datetime
    visibility_rate: Decimal | None  # None = missing/gap, NOT zero
    owned_citation_rate: Decimal | None

    def to_chart_dict(self) -> dict[str, object]:
        """Convert to a JSON-safe dict for Chart.js rendering."""
        return {
            "scan_id": str(self.scan_id),
            "created_at": self.created_at.isoformat(),
            "visibility_rate": float(self.visibility_rate)
            if self.visibility_rate is not None
            else None,
            "owned_citation_rate": float(self.owned_citation_rate)
            if self.owned_citation_rate is not None
            else None,
        }


@dataclass(frozen=True)
class LeaderboardEntry:
    """One entry in the brand vs competitors leaderboard."""

    entity_snapshot_id: uuid.UUID
    entity_type: str
    name: str
    domain: str
    visibility_rate: Decimal | None
    share_of_voice: Decimal | None


@dataclass(frozen=True)
class ProjectDashboardData:
    """All data needed to render the project dashboard page."""

    project: Project
    latest_scan: Scan | None
    latest_metrics: MetricsResult | None
    trend: list[TrendPoint]
    leaderboard: list[LeaderboardEntry]
    recent_scans: list[ScanHistoryItem]
    high_open_opportunities: int
    schedule: ProjectScanSchedule | None
    is_prompt_set_stale: bool
    standard_scan_estimate: int
    quota: QuotaSummary
    confidence_scans_enabled: bool = False
    min_scheduled_scan_interval_hours: int | None = None

    @property
    def chart_trend(self) -> list[dict[str, object]]:
        """JSON-safe trend data for Chart.js rendering."""
        return [p.to_chart_dict() for p in self.trend]


_ELIGIBLE_SCAN_STATUSES = (ScanStatus.COMPLETED, ScanStatus.PARTIAL)
_STANDARD_SCAN_TYPES = (ScanType.STANDARD,)


class DashboardQueryService:
    """Read-only dashboard data orchestration.

    All methods are read-only. No writes, no provider calls, no AI Checks.
    """

    def __init__(
        self,
        session: Session,
        *,
        entitlement_service: EntitlementService | None = None,
        metrics_service: VisibilityMetricsService | None = None,
    ) -> None:
        self._session = session
        self._entitlements = entitlement_service or EntitlementService(session)
        self._metrics = metrics_service or VisibilityMetricsService(session)

    # ------------------------------------------------------------------
    # Workspace overview
    # ------------------------------------------------------------------

    def get_workspace_overview(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> WorkspaceOverview:
        """Build the workspace landing page data."""
        # Active projects
        projects = self._list_active_projects(workspace_id)
        active_count = sum(1 for p in projects if p.status == ProjectStatus.ACTIVE)
        total_count = len(projects)

        # Quota
        quota = self._get_quota_summary(workspace_id)

        # High open opportunities
        high_opp_count = (
            self._session.execute(
                select(func.count(Opportunity.id)).where(
                    Opportunity.workspace_id == workspace_id,
                    Opportunity.priority == OpportunityPriority.HIGH,
                    Opportunity.status.in_([OpportunityStatus.OPEN, OpportunityStatus.IN_PROGRESS]),
                )
            ).scalar()
            or 0
        )

        # Unread notifications
        unread = (
            self._session.execute(
                select(func.count(Notification.id)).where(
                    Notification.workspace_id == workspace_id,
                    Notification.user_id == user_id,
                    Notification.read_at.is_(None),
                )
            ).scalar()
            or 0
        )

        # Next scheduled run
        next_run = self._get_next_scheduled_run(workspace_id)

        # Plan name
        ent = self._entitlements.get_effective_entitlements(workspace_id)
        plan_name = ent.plan_code if ent.plan_code != "UNENTITLED" else ""

        # Project cards
        cards = [self._build_project_card(p, workspace_id) for p in projects[:20]]

        return WorkspaceOverview(
            active_projects=active_count,
            total_projects=total_count,
            quota=quota,
            high_open_opportunities=high_opp_count,
            unread_notifications=unread,
            next_scheduled_run=next_run,
            projects=cards,
            plan_name=plan_name,
        )

    # ------------------------------------------------------------------
    # Project dashboard
    # ------------------------------------------------------------------

    def get_project_dashboard(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> ProjectDashboardData:
        """Build the project dashboard page data."""
        project = self._session.get(Project, project_id)
        if project is None or project.workspace_id != workspace_id:
            from app.core.exceptions import NotFoundError

            raise NotFoundError("Project not found.")

        # Latest eligible STANDARD scan
        latest_scan = self._latest_eligible_standard_scan(workspace_id, project_id)
        latest_metrics = None
        if latest_scan is not None:
            with contextlib.suppress(Exception):
                latest_metrics = self._metrics.get_metrics(
                    workspace_id, project_id, latest_scan.id, PromptType.NON_BRANDED
                )

        # Trend (up to 12 recent eligible standard scans)
        trend = self._get_visibility_trend(workspace_id, project_id)

        # Leaderboard from latest metrics
        leaderboard: list[LeaderboardEntry] = []
        if latest_metrics is not None:
            for entry in latest_metrics.leaderboard:
                leaderboard.append(
                    LeaderboardEntry(
                        entity_snapshot_id=entry.entity_snapshot_id,
                        entity_type=entry.entity_type,
                        name=entry.name,
                        domain=entry.domain,
                        visibility_rate=entry.visibility_rate,
                        share_of_voice=entry.share_of_voice,
                    )
                )

        # Recent scans
        recent_scans = self._get_recent_scans(workspace_id, project_id, limit=10)

        # High open opportunities for this project
        high_opp_count = (
            self._session.execute(
                select(func.count(Opportunity.id)).where(
                    Opportunity.workspace_id == workspace_id,
                    Opportunity.project_id == project_id,
                    Opportunity.priority == OpportunityPriority.HIGH,
                    Opportunity.status.in_([OpportunityStatus.OPEN, OpportunityStatus.IN_PROGRESS]),
                )
            ).scalar()
            or 0
        )

        # Schedule
        schedule = self._session.execute(
            select(ProjectScanSchedule).where(
                ProjectScanSchedule.project_id == project_id,
                ProjectScanSchedule.workspace_id == workspace_id,
            )
        ).scalar_one_or_none()

        # Prompt set staleness
        is_stale = self._check_prompt_set_stale(workspace_id, project_id)

        # Scan estimate
        estimate = self._get_scan_estimate(workspace_id, project_id)

        # Quota
        quota = self._get_quota_summary(workspace_id)

        # Entitlement flags for UI
        ent = self._entitlements.get_effective_entitlements(workspace_id)

        return ProjectDashboardData(
            project=project,
            latest_scan=latest_scan,
            latest_metrics=latest_metrics,
            trend=trend,
            leaderboard=leaderboard,
            recent_scans=recent_scans,
            high_open_opportunities=high_opp_count,
            schedule=schedule,
            is_prompt_set_stale=is_stale,
            standard_scan_estimate=estimate,
            quota=quota,
            confidence_scans_enabled=ent.confidence_scans_enabled,
            min_scheduled_scan_interval_hours=ent.min_scheduled_scan_interval_hours,
        )

    # ------------------------------------------------------------------
    # Scan status (for polling)
    # ------------------------------------------------------------------

    def get_scan_status(self, workspace_id: uuid.UUID, scan_id: uuid.UUID) -> Scan | None:
        """Return the scan for status polling. Read-only."""
        scan = self._session.get(Scan, scan_id)
        if scan is None or scan.workspace_id != workspace_id:
            return None
        return scan

    # ------------------------------------------------------------------
    # Scan detail
    # ------------------------------------------------------------------

    def get_scan_detail(self, workspace_id: uuid.UUID, scan_id: uuid.UUID) -> Scan | None:
        """Return a scan for detail view. Read-only."""
        scan = self._session.get(Scan, scan_id)
        if scan is None or scan.workspace_id != workspace_id:
            return None
        return scan

    def get_scan_evidence(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, scan_id: uuid.UUID
    ) -> list[dict[str, object]]:
        """Return PromptRun evidence for a scan, scoped by workspace+project+scan."""
        from app.services.scan_query_service import ScanQueryService

        svc = ScanQueryService(self._session)
        try:
            runs = svc.list_runs(workspace_id, project_id, scan_id)
        except Exception:
            return []
        evidence: list[dict[str, object]] = []
        for run in runs:
            evidence.append(
                {
                    "id": str(run.id),
                    "provider": run.provider.value
                    if hasattr(run.provider, "value")
                    else str(run.provider),
                    "prompt_type": str(run.prompt.prompt_type)
                    if hasattr(run, "prompt") and run.prompt is not None
                    else "",
                    "status": run.status.value if hasattr(run.status, "value") else str(run.status),
                    "response_text": run.response_text or "",
                    "error_code": run.error_code,
                    "sources": [{"title": s.title, "url": s.url} for s in (run.sources or [])]
                    if hasattr(run, "sources") and run.sources
                    else [],
                }
            )
        return evidence

    def get_scan_metrics_summary(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, scan_id: uuid.UUID
    ) -> dict[str, object]:
        """Return a summary of scan metrics for display."""
        with contextlib.suppress(Exception):
            metrics = self._metrics.get_metrics(
                workspace_id, project_id, scan_id, PromptType.NON_BRANDED
            )
            brand = next((e for e in metrics.entity_metrics if e.entity_type == "BRAND"), None)
            return {
                "planned_observations": metrics.planned_observations,
                "successful_observations": metrics.successful_observations,
                "failed_observations": metrics.planned_observations
                - metrics.successful_observations,
                "measurement_coverage": float(metrics.measurement_coverage)
                if metrics.measurement_coverage is not None
                else None,
                "brand_visibility_rate": float(brand.visibility_rate)
                if brand and brand.visibility_rate is not None
                else None,
                "brand_citation_rate": float(brand.owned_citation_rate)
                if brand and brand.owned_citation_rate is not None
                else None,
            }
        return {}

    def get_confidence_result(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, scan_id: uuid.UUID
    ) -> dict[str, object] | None:
        """Return confidence (reliability) metrics for a CONFIDENCE scan."""
        with contextlib.suppress(Exception):
            from app.services.confidence_metrics_service import ConfidenceMetricsService

            svc = ConfidenceMetricsService(self._session)
            result = svc.get_metrics(workspace_id, project_id, scan_id, PromptType.NON_BRANDED)
            brand = next(
                (e for e in result.entity_reliability if e.entity_type == "BRAND"),
                None,
            )
            return {
                "overall_confidence_level": result.overall_confidence_level,
                "repeat_count": result.repeat_count,
                "planned_observations": result.planned_observations,
                "successful_observations": result.successful_observations,
                "measurement_coverage": float(result.measurement_coverage)
                if result.measurement_coverage is not None
                else None,
                "brand_confidence_level": brand.confidence_level if brand else None,
                "brand_repeat_sufficiency": float(brand.repeat_sufficiency)
                if brand and brand.repeat_sufficiency is not None
                else None,
                "brand_mention_stability": float(brand.mention_stability)
                if brand and brand.mention_stability is not None
                else None,
                "brand_observed_range": float(brand.observed_visibility_range)
                if brand and brand.observed_visibility_range is not None
                else None,
            }
        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _list_active_projects(self, workspace_id: uuid.UUID) -> list[Project]:
        return list(
            self._session.execute(
                select(Project)
                .where(
                    Project.workspace_id == workspace_id,
                    Project.status.in_([ProjectStatus.ACTIVE, ProjectStatus.PAUSED]),
                )
                .order_by(Project.created_at.desc())
            ).scalars()
        )

    def _build_project_card(self, project: Project, workspace_id: uuid.UUID) -> ProjectCard:
        latest_scan = self._latest_eligible_standard_scan(workspace_id, project.id)
        visibility = None
        coverage = None
        if latest_scan is not None:
            with contextlib.suppress(Exception):
                metrics = self._metrics.get_metrics(
                    workspace_id, project.id, latest_scan.id, PromptType.NON_BRANDED
                )
                brand = next((e for e in metrics.entity_metrics if e.entity_type == "BRAND"), None)
                if brand is not None:
                    visibility = brand.visibility_rate
                coverage = metrics.measurement_coverage

        high_opp = (
            self._session.execute(
                select(func.count(Opportunity.id)).where(
                    Opportunity.workspace_id == workspace_id,
                    Opportunity.project_id == project.id,
                    Opportunity.priority == OpportunityPriority.HIGH,
                    Opportunity.status.in_([OpportunityStatus.OPEN, OpportunityStatus.IN_PROGRESS]),
                )
            ).scalar()
            or 0
        )

        next_run = self._get_next_scheduled_run_for_project(project.id)
        is_stale = self._check_prompt_set_stale(workspace_id, project.id)

        return ProjectCard(
            id=project.id,
            name=project.name,
            domain=project.domain,
            brand_name=project.brand_name,
            status=project.status,
            last_scan_at=project.last_scan_at,
            latest_scan_status=latest_scan.status if latest_scan else None,
            latest_scan_id=latest_scan.id if latest_scan else None,
            visibility_rate=visibility,
            measurement_coverage=coverage,
            high_open_opportunities=high_opp,
            next_scheduled_run=next_run,
            is_prompt_set_stale=is_stale,
        )

    def _latest_eligible_standard_scan(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> Scan | None:
        """Return the most recent STANDARD scan with COMPLETED analysis.

        If the latest terminal scan has a FAILED/PENDING/missing analysis,
        we fall back to the previous eligible scan.
        """
        scans = list(
            self._session.execute(
                select(Scan)
                .where(
                    Scan.workspace_id == workspace_id,
                    Scan.project_id == project_id,
                    Scan.scan_type.in_(_STANDARD_SCAN_TYPES),
                    Scan.status.in_(_ELIGIBLE_SCAN_STATUSES),
                )
                .order_by(Scan.created_at.desc())
                .limit(20)
            ).scalars()
        )
        for scan in scans:
            analysis = self._session.execute(
                select(ScanAnalysis).where(
                    ScanAnalysis.scan_id == scan.id,
                    ScanAnalysis.status == ScanAnalysisStatus.COMPLETED,
                )
            ).scalar_one_or_none()
            if analysis is not None:
                return scan
        return None

    def _recent_eligible_standard_scans(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, limit: int = 12
    ) -> list[Scan]:
        """Return recent STANDARD scans with COMPLETED analysis for trend."""
        scans = list(
            self._session.execute(
                select(Scan)
                .where(
                    Scan.workspace_id == workspace_id,
                    Scan.project_id == project_id,
                    Scan.scan_type.in_(_STANDARD_SCAN_TYPES),
                    Scan.status.in_(_ELIGIBLE_SCAN_STATUSES),
                )
                .order_by(Scan.created_at.desc())
                .limit(limit * 3)
            ).scalars()
        )
        eligible: list[Scan] = []
        for scan in scans:
            analysis = self._session.execute(
                select(ScanAnalysis).where(
                    ScanAnalysis.scan_id == scan.id,
                    ScanAnalysis.status == ScanAnalysisStatus.COMPLETED,
                )
            ).scalar_one_or_none()
            if analysis is not None:
                eligible.append(scan)
                if len(eligible) >= limit:
                    break
        return eligible

    def _get_visibility_trend(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> list[TrendPoint]:
        scans = self._recent_eligible_standard_scans(workspace_id, project_id, limit=12)
        # Reverse so oldest is first for chart display
        scans.reverse()
        points: list[TrendPoint] = []
        for scan in scans:
            visibility = None
            citation = None
            with contextlib.suppress(Exception):
                metrics = self._metrics.get_metrics(
                    workspace_id, project_id, scan.id, PromptType.NON_BRANDED
                )
                brand = next((e for e in metrics.entity_metrics if e.entity_type == "BRAND"), None)
                if brand is not None:
                    visibility = brand.visibility_rate
                    citation = brand.owned_citation_rate
            points.append(
                TrendPoint(
                    scan_id=scan.id,
                    created_at=scan.created_at,
                    visibility_rate=visibility,
                    owned_citation_rate=citation,
                )
            )
        return points

    def _get_recent_scans(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, limit: int = 10
    ) -> list[ScanHistoryItem]:
        scans = list(
            self._session.execute(
                select(Scan)
                .where(
                    Scan.workspace_id == workspace_id,
                    Scan.project_id == project_id,
                )
                .order_by(Scan.created_at.desc())
                .limit(limit)
            ).scalars()
        )
        items: list[ScanHistoryItem] = []
        for scan in scans:
            from app.core.enums import PromptRunStatus
            from app.models.scan import PromptRun

            planned = (
                self._session.execute(
                    select(func.count(PromptRun.id)).where(PromptRun.scan_id == scan.id)
                ).scalar()
                or 0
            )
            succeeded = (
                self._session.execute(
                    select(func.count(PromptRun.id)).where(
                        PromptRun.scan_id == scan.id,
                        PromptRun.status == PromptRunStatus.SUCCEEDED,
                    )
                ).scalar()
                or 0
            )
            failed = (
                self._session.execute(
                    select(func.count(PromptRun.id)).where(
                        PromptRun.scan_id == scan.id,
                        PromptRun.status == PromptRunStatus.FAILED,
                    )
                ).scalar()
                or 0
            )
            coverage = None
            if planned > 0:
                coverage = Decimal(succeeded) / Decimal(planned) * Decimal(100)
                coverage = coverage.quantize(Decimal("0.01"))

            origin = self._scan_origin_label(scan)

            items.append(
                ScanHistoryItem(
                    id=scan.id,
                    scan_type=scan.scan_type,
                    status=scan.status,
                    created_at=scan.created_at,
                    completed_at=scan.completed_at,
                    successful_observations=succeeded,
                    failed_observations=failed,
                    planned_observations=planned,
                    coverage=coverage,
                    origin=origin,
                )
            )
        return items

    def _scan_origin_label(self, scan: Scan) -> str:
        """Translate scan origin to customer-friendly label."""

        if scan.scan_schedule_id is not None:
            return "Scheduled"
        if scan.scan_type == ScanType.VERIFICATION:
            return "Verification"
        if scan.scan_type == ScanType.CONFIDENCE:
            return "Reliability check"
        return "Manual"

    def _get_quota_summary(self, workspace_id: uuid.UUID) -> QuotaSummary:
        from app.models.workspace_usage_period import WorkspaceUsagePeriod
        from app.services.quota_service import month_period

        ent = self._entitlements.get_effective_entitlements(workspace_id)
        limit = ent.monthly_ai_checks
        period_start, _ = month_period()
        period = self._session.execute(
            select(WorkspaceUsagePeriod).where(
                WorkspaceUsagePeriod.workspace_id == workspace_id,
                WorkspaceUsagePeriod.period_start == period_start,
            )
        ).scalar_one_or_none()
        if period is None:
            return QuotaSummary(used=0, reserved=0, limit=limit)
        return QuotaSummary(
            used=period.ai_checks_used,
            reserved=period.ai_checks_reserved,
            limit=limit,
        )

    def _get_next_scheduled_run(self, workspace_id: uuid.UUID) -> datetime | None:
        result = self._session.execute(
            select(func.min(ProjectScanSchedule.next_run_at)).where(
                ProjectScanSchedule.workspace_id == workspace_id,
                ProjectScanSchedule.enabled.is_(True),
                ProjectScanSchedule.next_run_at.is_not(None),
            )
        ).scalar()
        return result

    def _get_next_scheduled_run_for_project(self, project_id: uuid.UUID) -> datetime | None:
        result = self._session.execute(
            select(func.min(ProjectScanSchedule.next_run_at)).where(
                ProjectScanSchedule.project_id == project_id,
                ProjectScanSchedule.enabled.is_(True),
                ProjectScanSchedule.next_run_at.is_not(None),
            )
        ).scalar()
        return result

    def _check_prompt_set_stale(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> bool:
        from app.core.enums import PromptSetStatus
        from app.models.prompt_set import PromptSet

        project = self._session.get(Project, project_id)
        if project is None:
            return False
        active_set = self._session.execute(
            select(PromptSet).where(
                PromptSet.project_id == project_id,
                PromptSet.status == PromptSetStatus.ACTIVE,
            )
        ).scalar_one_or_none()
        if active_set is None:
            return True
        return active_set.input_revision != project.prompt_input_revision

    def _get_scan_estimate(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> int:
        """Estimate AI Checks for a standard scan (planned prompts)."""
        from app.core.enums import PromptSetStatus
        from app.models.prompt_set import PromptSet
        from app.models.tracking import ProjectProvider, Prompt

        active_set = self._session.execute(
            select(PromptSet).where(
                PromptSet.project_id == project_id,
                PromptSet.status == PromptSetStatus.ACTIVE,
            )
        ).scalar_one_or_none()
        if active_set is None:
            return 0
        prompt_count = (
            self._session.execute(
                select(func.count(Prompt.id)).where(Prompt.prompt_set_id == active_set.id)
            ).scalar()
            or 0
        )
        provider_count = (
            self._session.execute(
                select(func.count(ProjectProvider.id)).where(
                    ProjectProvider.project_id == project_id,
                    ProjectProvider.enabled.is_(True),
                )
            ).scalar()
            or 0
        )
        return prompt_count * provider_count if provider_count > 0 else 0
