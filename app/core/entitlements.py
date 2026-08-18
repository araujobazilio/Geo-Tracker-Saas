"""Effective entitlements and usage snapshot value objects.

These are immutable typed objects that the rest of the application
consumes. Routers and services never interact with billing tables
directly — they use these objects.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import NamedTuple

from app.core.enums import BillingSource, LLMProvider


class EffectiveEntitlements(NamedTuple):
    """Immutable snapshot of what a workspace is entitled to.

    `plan_code == "UNENTITLED"` means the workspace has no valid
    billing/plan and all paid capabilities are zero/disabled.
    """

    workspace_id: uuid.UUID
    plan_code: str
    billing_source: BillingSource | None

    # Resource limits
    max_projects: int
    max_keywords_per_project: int
    max_competitors_per_project: int
    max_team_members: int

    # Usage limit (always finite, never unlimited)
    monthly_ai_checks: int

    # Allowed providers (empty = no providers allowed)
    allowed_providers: frozenset[LLMProvider]

    # Scan capabilities
    min_scheduled_scan_interval_hours: int | None
    confidence_scans_enabled: bool
    verification_scans_enabled: bool

    # Feature flags
    white_label_reports: bool
    exports_enabled: bool
    agency_dashboard: bool
    integrations_enabled: bool
    byok_enabled: bool

    @property
    def is_unentitled(self) -> bool:
        return self.plan_code == "UNENTITLED"

    @classmethod
    def unentitled(cls, workspace_id: uuid.UUID) -> EffectiveEntitlements:
        """Return a conservative UNENTITLED snapshot — fail closed."""
        return cls(
            workspace_id=workspace_id,
            plan_code="UNENTITLED",
            billing_source=None,
            max_projects=0,
            max_keywords_per_project=0,
            max_competitors_per_project=0,
            max_team_members=0,
            monthly_ai_checks=0,
            allowed_providers=frozenset(),
            min_scheduled_scan_interval_hours=None,
            confidence_scans_enabled=False,
            verification_scans_enabled=False,
            white_label_reports=False,
            exports_enabled=False,
            agency_dashboard=False,
            integrations_enabled=False,
            byok_enabled=False,
        )


class UsageSnapshot(NamedTuple):
    """Immutable snapshot of current monthly quota usage.

    Available = limit - used - reserved (clamped at >= 0).
    """

    workspace_id: uuid.UUID
    period_start: datetime
    period_end: datetime
    limit: int
    used: int
    reserved: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used - self.reserved)

    @property
    def usage_percentage(self) -> int:
        """Integer percentage (0-100). Avoids floating-point issues."""
        if self.limit <= 0:
            return 0
        return min(100, int((self.used / self.limit) * 100))
