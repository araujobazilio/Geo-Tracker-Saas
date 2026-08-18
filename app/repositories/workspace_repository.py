"""Workspace and WorkspaceMember repositories."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import WorkspaceRole
from app.models.workspace import Workspace, WorkspaceMember


class WorkspaceRepository:
    """Persistence layer for Workspace entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, workspace_id: uuid.UUID) -> Workspace | None:
        return self._session.get(Workspace, workspace_id)

    def get_for_update(self, workspace_id: uuid.UUID) -> Workspace | None:
        """Lock the workspace row for update (for capacity checks)."""
        result = self._session.execute(
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    def list_for_user(self, user_id: uuid.UUID) -> list[Workspace]:
        """Return all workspaces where `user_id` is a member."""
        stmt = (
            select(Workspace)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(WorkspaceMember.user_id == user_id)
            .order_by(Workspace.created_at)
        )
        return list(self._session.execute(stmt).scalars().all())

    def create(self, workspace: Workspace) -> Workspace:
        self._session.add(workspace)
        self._session.flush()
        return workspace

    def update(self, workspace: Workspace) -> Workspace:
        self._session.flush()
        return workspace


class WorkspaceMemberRepository:
    """Persistence layer for WorkspaceMember entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_membership(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceMember | None:
        stmt = select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def list_members(self, workspace_id: uuid.UUID) -> list[WorkspaceMember]:
        stmt = select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
        return list(self._session.execute(stmt).scalars().all())

    def create(self, member: WorkspaceMember) -> WorkspaceMember:
        self._session.add(member)
        self._session.flush()
        return member

    def get_role(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceRole | None:
        member = self.get_membership(workspace_id, user_id)
        if member is None:
            return None
        return WorkspaceRole(member.role)
