"""ProjectProvider and PromptSet/Prompt repositories."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import LLMProvider, PromptSetStatus
from app.models.prompt_set import PromptSet
from app.models.tracking import ProjectProvider, Prompt


class ProjectProviderRepository:
    """Persistence layer for ProjectProvider entities (project-scoped)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_by_project(self, project_id: uuid.UUID) -> list[ProjectProvider]:
        return list(
            self._session.execute(
                select(ProjectProvider)
                .where(ProjectProvider.project_id == project_id)
                .order_by(ProjectProvider.provider)
            ).scalars()
        )

    def list_enabled_by_project(self, project_id: uuid.UUID) -> list[ProjectProvider]:
        return list(
            self._session.execute(
                select(ProjectProvider)
                .where(
                    ProjectProvider.project_id == project_id,
                    ProjectProvider.enabled.is_(True),
                )
                .order_by(ProjectProvider.provider)
            ).scalars()
        )

    def count_enabled_by_project(self, project_id: uuid.UUID) -> int:
        result = self._session.execute(
            select(func.count(ProjectProvider.id)).where(
                ProjectProvider.project_id == project_id,
                ProjectProvider.enabled.is_(True),
            )
        )
        return int(result.scalar() or 0)

    def delete_by_project(self, project_id: uuid.UUID) -> None:
        """Delete all provider rows for a project (used by PUT replace)."""
        self._session.execute(
            select(ProjectProvider).where(ProjectProvider.project_id == project_id)
        )
        rows = self.list_by_project(project_id)
        for row in rows:
            self._session.delete(row)
        self._session.flush()

    def create(self, provider: ProjectProvider) -> ProjectProvider:
        self._session.add(provider)
        self._session.flush()
        return provider

    def get_by_provider(
        self, project_id: uuid.UUID, provider: LLMProvider
    ) -> ProjectProvider | None:
        return self._session.execute(
            select(ProjectProvider).where(
                ProjectProvider.project_id == project_id,
                ProjectProvider.provider == provider,
            )
        ).scalar_one_or_none()


class PromptSetRepository:
    """Persistence layer for PromptSet entities (project-scoped)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, prompt_set_id: uuid.UUID) -> PromptSet | None:
        return self._session.get(PromptSet, prompt_set_id)

    def get_active_by_project(self, project_id: uuid.UUID) -> PromptSet | None:
        """Return the current ACTIVE prompt set for a project."""
        return self._session.execute(
            select(PromptSet).where(
                PromptSet.project_id == project_id,
                PromptSet.status == PromptSetStatus.ACTIVE,
            )
        ).scalar_one_or_none()

    def get_by_version(self, project_id: uuid.UUID, version: int) -> PromptSet | None:
        return self._session.execute(
            select(PromptSet).where(
                PromptSet.project_id == project_id,
                PromptSet.version == version,
            )
        ).scalar_one_or_none()

    def list_by_project(self, project_id: uuid.UUID) -> list[PromptSet]:
        return list(
            self._session.execute(
                select(PromptSet)
                .where(PromptSet.project_id == project_id)
                .order_by(PromptSet.version.desc())
            ).scalars()
        )

    def max_version_by_project(self, project_id: uuid.UUID) -> int:
        """Return the maximum version number for a project, or 0 if none."""
        result = self._session.execute(
            select(func.max(PromptSet.version)).where(PromptSet.project_id == project_id)
        )
        return int(result.scalar() or 0)

    def create(self, prompt_set: PromptSet) -> PromptSet:
        self._session.add(prompt_set)
        self._session.flush()
        return prompt_set


class PromptRepository:
    """Persistence layer for Prompt entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_by_prompt_set(self, prompt_set_id: uuid.UUID) -> list[Prompt]:
        """List prompts in a prompt set, ordered deterministically."""
        return list(
            self._session.execute(
                select(Prompt)
                .where(Prompt.prompt_set_id == prompt_set_id)
                .order_by(Prompt.project_keyword_id, Prompt.variant_index)
            ).scalars()
        )

    def count_by_prompt_set(self, prompt_set_id: uuid.UUID) -> int:
        result = self._session.execute(
            select(func.count(Prompt.id)).where(Prompt.prompt_set_id == prompt_set_id)
        )
        return int(result.scalar() or 0)

    def create(self, prompt: Prompt) -> Prompt:
        self._session.add(prompt)
        self._session.flush()
        return prompt

    def create_batch(self, prompts: list[Prompt]) -> list[Prompt]:
        self._session.add_all(prompts)
        self._session.flush()
        return prompts
