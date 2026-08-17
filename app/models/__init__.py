"""ORM models.

Importing this package registers all models on `Base.metadata` so that
Alembic autogenerate and `Base.metadata.create_all()` can see them.
"""

from __future__ import annotations

from app.models.audit import AuditLog
from app.models.billing import AppSumoLicense, BillingAccount
from app.models.project import Project
from app.models.tracking import Competitor, ProjectKeyword, ProjectProvider, Prompt
from app.models.usage import UsageEvent
from app.models.user import User
from app.models.webhook import ProviderWebhookEvent
from app.models.workspace import Workspace, WorkspaceMember

__all__ = [
    "AuditLog",
    "AppSumoLicense",
    "BillingAccount",
    "Competitor",
    "Project",
    "ProjectKeyword",
    "ProjectProvider",
    "Prompt",
    "ProviderWebhookEvent",
    "UsageEvent",
    "User",
    "Workspace",
    "WorkspaceMember",
]
