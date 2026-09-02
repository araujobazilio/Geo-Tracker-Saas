"""Template context builders for the web layer.

Provides a shared ``WebContext`` that every authenticated template
receives: current user, workspace, role, CSRF token, flash messages,
notification count, and quota summary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.enums import WorkspaceRole
from app.services.workspace_auth_service import WorkspaceAuthorizationService

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.workspace import Workspace


@dataclass
class FlashMessage:
    """A single flash/toast message rendered once after an action."""

    level: str  # "success", "error", "warning", "info"
    text: str


@dataclass
class QuotaSummary:
    """Workspace AI Check usage summary for display."""

    used: int = 0
    reserved: int = 0
    limit: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used - self.reserved)

    @property
    def usage_pct(self) -> float:
        if self.limit == 0:
            return 0.0
        return round((self.used + self.reserved) / self.limit * 100, 1)

    @property
    def is_warning(self) -> bool:
        return self.usage_pct >= 80.0


@dataclass
class WebContext:
    """The shared context object passed to every authenticated template."""

    user: User
    workspace: Workspace
    workspace_id: uuid.UUID
    role: WorkspaceRole
    csrf_token: str
    unread_notifications: int = 0
    quota: QuotaSummary = field(default_factory=QuotaSummary)
    flashes: list[FlashMessage] = field(default_factory=list)
    is_owner_or_admin: bool = False
    plan_name: str = ""
    extra: dict[str, object] = field(default_factory=dict)

    def add_flash(self, level: str, text: str) -> None:
        self.flashes.append(FlashMessage(level=level, text=text))

    def to_dict(self) -> dict[str, object]:
        """Convert to a plain dict for Jinja2 template rendering."""
        return {
            "user": self.user,
            "workspace": self.workspace,
            "workspace_id": str(self.workspace_id),
            "role": self.role.value,
            "csrf_token": self.csrf_token,
            "unread_notifications": self.unread_notifications,
            "quota": self.quota,
            "flashes": self.flashes,
            "is_owner_or_admin": self.is_owner_or_admin,
            "plan_name": self.plan_name,
            **self.extra,
        }


def resolve_role(
    auth_service: WorkspaceAuthorizationService,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> WorkspaceRole:
    """Return the user's role in the workspace, or MEMBER as fallback."""
    role = auth_service.get_role(workspace_id, user_id)
    return role if role is not None else WorkspaceRole.MEMBER
