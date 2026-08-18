"""Workspace service — business logic for workspace CRUD."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.enums import WorkspaceRole, WorkspaceType
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.models.workspace import Workspace, WorkspaceMember
from app.repositories.workspace_repository import WorkspaceMemberRepository, WorkspaceRepository
from app.services.audit_service import AuditService
from app.services.workspace_auth_service import WorkspaceAuthorizationService

logger = get_logger("app.workspace")


class WorkspaceService:
    """Workspace CRUD with tenant authorization."""

    def __init__(
        self,
        session: Session,
        audit_service: AuditService | None = None,
    ) -> None:
        self._session = session
        self._repo = WorkspaceRepository(session)
        self._member_repo = WorkspaceMemberRepository(session)
        self._audit = audit_service or AuditService()
        self._authz = WorkspaceAuthorizationService(session, audit_service=self._audit)

    def list_workspaces(self, user_id: uuid.UUID) -> list[Workspace]:
        """Return all workspaces where the user is a member."""
        return self._repo.list_for_user(user_id)

    def get_workspace(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> Workspace:
        """Return a workspace if the user is a member.

        Raises TenantAccessError (→ 404) for non-members.
        """
        self._authz.require_membership(workspace_id, user_id)
        ws = self._repo.get_by_id(workspace_id)
        if ws is None:
            raise NotFoundError("Workspace not found.")
        return ws

    def create_workspace(
        self,
        user_id: uuid.UUID,
        name: str,
        workspace_type: WorkspaceType,
    ) -> Workspace:
        """Create a workspace with the creator as OWNER. Atomic."""
        ws = Workspace(name=name, workspace_type=workspace_type)
        self._repo.create(ws)
        membership = WorkspaceMember(
            workspace_id=ws.id,
            user_id=user_id,
            role=WorkspaceRole.OWNER,
        )
        self._member_repo.create(membership)
        self._session.commit()
        self._audit.record(
            action="WORKSPACE_CREATED",
            user_id=user_id,
            workspace_id=ws.id,
            entity_type="workspace",
            entity_id=ws.id,
        )
        return ws

    def update_workspace(
        self,
        workspace_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str,
    ) -> Workspace:
        """Update a workspace name. Requires OWNER or ADMIN."""
        self._authz.require_role(workspace_id, user_id, WorkspaceRole.ADMIN)
        ws = self._repo.get_by_id(workspace_id)
        if ws is None:
            raise NotFoundError("Workspace not found.")
        ws.name = name
        self._repo.update(ws)
        self._session.commit()
        self._audit.record(
            action="WORKSPACE_UPDATED",
            user_id=user_id,
            workspace_id=ws.id,
            entity_type="workspace",
            entity_id=ws.id,
        )
        return ws

    def get_user_role(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceRole | None:
        """Return the user's role in a workspace, or None if not a member."""
        return self._authz.get_role(workspace_id, user_id)
