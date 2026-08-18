"""ProjectKeyword repository — project-scoped keyword lookups with locking."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.tracking import ProjectKeyword


class ProjectKeywordRepository:
    """Persistence layer for ProjectKeyword entities (project-scoped)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, keyword_id: uuid.UUID) -> ProjectKeyword | None:
        return self._session.get(ProjectKeyword, keyword_id)

    def get_in_project(self, keyword_id: uuid.UUID, project_id: uuid.UUID) -> ProjectKeyword | None:
        """Return the keyword only if it belongs to the given project."""
        return self._session.execute(
            select(ProjectKeyword).where(
                ProjectKeyword.id == keyword_id,
                ProjectKeyword.project_id == project_id,
            )
        ).scalar_one_or_none()

    def get_in_project_for_update(
        self, keyword_id: uuid.UUID, project_id: uuid.UUID
    ) -> ProjectKeyword | None:
        """Lock the keyword row for update (project-scoped)."""
        result = self._session.execute(
            select(ProjectKeyword)
            .where(
                ProjectKeyword.id == keyword_id,
                ProjectKeyword.project_id == project_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    def get_by_normalized_text(
        self, project_id: uuid.UUID, normalized_text: str
    ) -> ProjectKeyword | None:
        """Find a keyword by its normalized text within a project."""
        return self._session.execute(
            select(ProjectKeyword).where(
                ProjectKeyword.project_id == project_id,
                ProjectKeyword.normalized_text == normalized_text,
            )
        ).scalar_one_or_none()

    def list_by_project(self, project_id: uuid.UUID) -> list[ProjectKeyword]:
        """List all keywords in a project, ordered by created_at."""
        return list(
            self._session.execute(
                select(ProjectKeyword)
                .where(ProjectKeyword.project_id == project_id)
                .order_by(ProjectKeyword.created_at)
            ).scalars()
        )

    def list_active_by_project(self, project_id: uuid.UUID) -> list[ProjectKeyword]:
        """List active keywords in a project, ordered by created_at."""
        return list(
            self._session.execute(
                select(ProjectKeyword)
                .where(
                    ProjectKeyword.project_id == project_id,
                    ProjectKeyword.active.is_(True),
                )
                .order_by(ProjectKeyword.created_at)
            ).scalars()
        )

    def count_active_by_project(self, project_id: uuid.UUID) -> int:
        """Count active keywords in a project."""
        result = self._session.execute(
            select(func.count(ProjectKeyword.id)).where(
                ProjectKeyword.project_id == project_id,
                ProjectKeyword.active.is_(True),
            )
        )
        return int(result.scalar() or 0)

    def create(self, keyword: ProjectKeyword) -> ProjectKeyword:
        self._session.add(keyword)
        self._session.flush()
        return keyword
