"""Competitor repository — project-scoped competitor lookups with locking."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.tracking import Competitor


class CompetitorRepository:
    """Persistence layer for Competitor entities (project-scoped)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, competitor_id: uuid.UUID) -> Competitor | None:
        return self._session.get(Competitor, competitor_id)

    def get_in_project(self, competitor_id: uuid.UUID, project_id: uuid.UUID) -> Competitor | None:
        """Return the competitor only if it belongs to the given project."""
        return self._session.execute(
            select(Competitor).where(
                Competitor.id == competitor_id,
                Competitor.project_id == project_id,
            )
        ).scalar_one_or_none()

    def get_in_project_for_update(
        self, competitor_id: uuid.UUID, project_id: uuid.UUID
    ) -> Competitor | None:
        """Lock the competitor row for update (project-scoped)."""
        result = self._session.execute(
            select(Competitor)
            .where(
                Competitor.id == competitor_id,
                Competitor.project_id == project_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    def get_by_domain(self, project_id: uuid.UUID, domain: str) -> Competitor | None:
        """Find a competitor by its domain within a project."""
        return self._session.execute(
            select(Competitor).where(
                Competitor.project_id == project_id,
                Competitor.domain == domain,
            )
        ).scalar_one_or_none()

    def list_by_project(self, project_id: uuid.UUID) -> list[Competitor]:
        """List all competitors in a project, ordered by created_at."""
        return list(
            self._session.execute(
                select(Competitor)
                .where(Competitor.project_id == project_id)
                .order_by(Competitor.created_at)
            ).scalars()
        )

    def list_active_by_project(self, project_id: uuid.UUID) -> list[Competitor]:
        """List active competitors in a project, ordered by created_at."""
        return list(
            self._session.execute(
                select(Competitor)
                .where(
                    Competitor.project_id == project_id,
                    Competitor.active.is_(True),
                )
                .order_by(Competitor.created_at)
            ).scalars()
        )

    def count_active_by_project(self, project_id: uuid.UUID) -> int:
        """Count active competitors in a project."""
        result = self._session.execute(
            select(func.count(Competitor.id)).where(
                Competitor.project_id == project_id,
                Competitor.active.is_(True),
            )
        )
        return int(result.scalar() or 0)

    def create(self, competitor: Competitor) -> Competitor:
        self._session.add(competitor)
        self._session.flush()
        return competitor
