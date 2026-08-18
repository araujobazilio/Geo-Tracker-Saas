"""ORM models.

Importing this package registers all models on `Base.metadata` so that
Alembic autogenerate and `Base.metadata.create_all()` can see them.
"""

from __future__ import annotations

from app.models.audit import AuditLog
from app.models.billing import AppSumoLicense, BillingAccount
from app.models.plan_definition import PlanDefinition
from app.models.plan_provider import PlanProvider
from app.models.project import Project
from app.models.quota_reservation import QuotaReservation
from app.models.tracking import Competitor, ProjectKeyword, ProjectProvider, Prompt
from app.models.usage import UsageEvent
from app.models.user import User
from app.models.webhook import ProviderWebhookEvent
from app.models.workspace import Workspace, WorkspaceMember
from app.models.workspace_usage_period import WorkspaceUsagePeriod

__all__ = [
    "AuditLog",
    "AppSumoLicense",
    "BillingAccount",
    "Competitor",
    "PlanDefinition",
    "PlanProvider",
    "Project",
    "ProjectKeyword",
    "ProjectProvider",
    "Prompt",
    "ProviderWebhookEvent",
    "QuotaReservation",
    "UsageEvent",
    "User",
    "Workspace",
    "WorkspaceMember",
    "WorkspaceUsagePeriod",
]
