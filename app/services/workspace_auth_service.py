"""Workspace authorization service — centralized tenant access enforcement.

This service is the single point of authorization for workspace-scoped
resources. It enforces:
  - membership: the user must be a member of the workspace
  - role: the user must have a sufficient role for the operation

Tenant isolation policy:
  If a user is not a member of a workspace, the service raises
  TenantAccessError. Routers translate this to HTTP 404 (not 403) to
  avoid revealing whether an inaccessible resource exists.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.enums import WorkspaceRole
from app.core.exceptions import TenantAccessError
from app.models.workspace import WorkspaceMember
from app.repositories.workspace_repository import WorkspaceMemberRepository

# Role hierarchy for capability checks: OWNER > ADMIN > MEMBER.
_ROLE_LEVEL: dict[WorkspaceRole, int] = {
    WorkspaceRole.MEMBER: 1,
    WorkspaceRole.ADMIN: 2,
    WorkspaceRole.OWNER: 3,
}


class WorkspaceAuthorizationService:
    """Centralized workspace membership and role authorization."""

    def __init__(self, session: Session) -> None:
        self._member_repo = WorkspaceMemberRepository(session)

    def require_membership(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceMember:
        """Return the membership if the user belongs to the workspace.

        Raises TenantAccessError if not a member.
        """
        member = self._member_repo.get_membership(workspace_id, user_id)
        if member is None:
            raise TenantAccessError("Resource not found.")
        return member

    def require_role(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID, min_role: WorkspaceRole
    ) -> WorkspaceMember:
        """Require that the user is a member with at least `min_role`.

        Raises TenantAccessError for non-members.
        Raises AuthorizationError for insufficient role.
        """
        from app.core.exceptions import AuthorizationError

        member = self.require_membership(workspace_id, user_id)
        member_role = WorkspaceRole(member.role)
        if _ROLE_LEVEL[member_role] < _ROLE_LEVEL[min_role]:
            raise AuthorizationError("Insufficient permissions.")
        return member

    def can_manage_workspace(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Return True if the user can modify workspace settings (OWNER or ADMIN)."""
        member = self._member_repo.get_membership(workspace_id, user_id)
        if member is None:
            return False
        return _ROLE_LEVEL[WorkspaceRole(member.role)] >= _ROLE_LEVEL[WorkspaceRole.ADMIN]

    def get_role(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceRole | None:
        return self._member_repo.get_role(workspace_id, user_id)

    def is_member(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return self._member_repo.get_membership(workspace_id, user_id) is not None
