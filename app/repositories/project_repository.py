"""Project repository — tenant-scoped project lookups."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project import Project


class ProjectRepository:
    """Persistence layer for Project entities (tenant-scoped)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, project_id: uuid.UUID) -> Project | None:
        return self._session.get(Project, project_id)

    def get_in_workspace(self, project_id: uuid.UUID, workspace_id: uuid.UUID) -> Project | None:
        """Return the project only if it belongs to the given workspace.

        Returns None if the project does not exist or belongs to a
        different workspace. This is the tenant-scoped lookup used by
        quota operations to prevent cross-workspace project linkage.
        """
        return self._session.execute(
            select(Project).where(
                Project.id == project_id,
                Project.workspace_id == workspace_id,
            )
        ).scalar_one_or_none()
