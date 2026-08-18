"""Project repository — tenant-scoped project lookups with locking support."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import ProjectStatus
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
        protected operations to prevent cross-workspace access.
        """
        return self._session.execute(
            select(Project).where(
                Project.id == project_id,
                Project.workspace_id == workspace_id,
            )
        ).scalar_one_or_none()

    def get_in_workspace_for_update(
        self, project_id: uuid.UUID, workspace_id: uuid.UUID
    ) -> Project | None:
        """Lock the project row for update (tenant-scoped)."""
        result = self._session.execute(
            select(Project)
            .where(
                Project.id == project_id,
                Project.workspace_id == workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    def list_by_workspace(self, workspace_id: uuid.UUID) -> list[Project]:
        """List all projects in a workspace, ordered by created_at."""
        return list(
            self._session.execute(
                select(Project)
                .where(Project.workspace_id == workspace_id)
                .order_by(Project.created_at)
            ).scalars()
        )

    def count_tracked_by_workspace(self, workspace_id: uuid.UUID) -> int:
        """Count ACTIVE + PAUSED projects (tracked for plan capacity).

        ARCHIVED projects do not consume active project capacity.
        """
        result = self._session.execute(
            select(func.count(Project.id)).where(
                Project.workspace_id == workspace_id,
                Project.status.in_([ProjectStatus.ACTIVE, ProjectStatus.PAUSED]),
            )
        )
        return int(result.scalar() or 0)

    def create(self, project: Project) -> Project:
        self._session.add(project)
        self._session.flush()
        return project
