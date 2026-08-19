"""PromptSet service — versioned prompt set lifecycle management.

Responsibilities:
  - Generate initial PromptSet v1 during project onboarding
  - Regenerate PromptSets when project configuration changes
  - Manage ACTIVE/SUPERSEDED status transitions
  - Track staleness via input_revision

Concurrency: regeneration locks the Project row (FOR UPDATE) to prevent
two concurrent regenerations from creating conflicting versions or two
ACTIVE prompt sets.

Historical retention: old PromptSets and their Prompt rows are never
modified or deleted. SUPERSEDED sets remain available forever for
reproducible scans.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import PromptSetStatus
from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.models.project import Project
from app.models.prompt_set import PromptSet
from app.models.tracking import Competitor, ProjectKeyword, Prompt
from app.repositories.project_repository import ProjectRepository
from app.repositories.tracking_repository import (
    PromptRepository,
    PromptSetRepository,
)
from app.services.prompt_generation_service import (
    GENERATOR_KEY,
    PromptGenerationService,
)

logger = get_logger("app.prompt_set")


class PromptSetService:
    """Versioned prompt set lifecycle management."""

    def __init__(
        self,
        session: Session,
        generation_service: PromptGenerationService | None = None,
    ) -> None:
        self._session = session
        self._project_repo = ProjectRepository(session)
        self._prompt_set_repo = PromptSetRepository(session)
        self._prompt_repo = PromptRepository(session)
        self._generation_service = generation_service or PromptGenerationService()

    def generate_initial_prompt_set(
        self,
        project: Project,
        keywords: list[ProjectKeyword],
        competitors: list[Competitor],
        created_by_user_id: uuid.UUID | None = None,
    ) -> PromptSet:
        """Generate the initial PromptSet v1 for a new project.

        Called during atomic onboarding. The project must have at least
        one active keyword.

        The new PromptSet will have:
          - version = 1
          - input_revision = project.prompt_input_revision
          - status = ACTIVE
          - generator_key = deterministic-template-v2
        """
        return self._create_prompt_set(
            project=project,
            keywords=keywords,
            competitors=competitors,
            version=1,
            created_by_user_id=created_by_user_id,
        )

    def regenerate_prompt_set(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        created_by_user_id: uuid.UUID | None = None,
    ) -> PromptSet:
        """Regenerate the prompt set for a project.

        Locks the Project row (FOR UPDATE) to prevent concurrent
        regenerations from creating conflicting versions.

        Behavior:
          - If current ACTIVE set is fresh (input_revision matches) and
            generator_key is current → return existing set (no new version).
          - If stale → create next version, supersede old ACTIVE set,
            activate new set.

        Raises:
            ConflictError: if project not found or no active keywords.
            ValidationError: if generation fails.
        """
        try:
            # Lock the project row.
            project = self._project_repo.get_in_workspace_for_update(project_id, workspace_id)
            if project is None:
                raise ConflictError("Project not found.")

            # Check current active set.
            current = self._prompt_set_repo.get_active_by_project(project.id)
            if current is not None:
                is_fresh = current.input_revision == project.prompt_input_revision
                is_current_generator = current.generator_key == GENERATOR_KEY
                if is_fresh and is_current_generator:
                    # Already fresh — return existing, no new version.
                    self._session.commit()
                    return current

            # Load active keywords and competitors.
            keywords = self._load_active_keywords(project.id)
            competitors = self._load_active_competitors(project.id)

            if not keywords:
                raise ConflictError("Cannot regenerate prompts: project has no active keywords.")

            # Determine next version.
            next_version = self._prompt_set_repo.max_version_by_project(project.id) + 1

            # Supersede current active set.
            if current is not None:
                current.status = PromptSetStatus.SUPERSEDED

            # Create new prompt set.
            new_set = self._create_prompt_set(
                project=project,
                keywords=keywords,
                competitors=competitors,
                version=next_version,
                created_by_user_id=created_by_user_id,
            )

            self._session.commit()
            return new_set

        except (ConflictError, ValidationError):
            self._session.rollback()
            raise

    def get_current_prompt_set(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> PromptSet | None:
        """Return the current ACTIVE prompt set for a project, or None."""
        project = self._project_repo.get_in_workspace(project_id, workspace_id)
        if project is None:
            return None
        return self._prompt_set_repo.get_active_by_project(project.id)

    def get_prompt_set_by_version(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, version: int
    ) -> PromptSet | None:
        """Return a specific prompt set version for a project."""
        project = self._project_repo.get_in_workspace(project_id, workspace_id)
        if project is None:
            return None
        return self._prompt_set_repo.get_by_version(project.id, version)

    def list_prompt_sets(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> list[PromptSet]:
        """List all prompt sets for a project, newest first."""
        project = self._project_repo.get_in_workspace(project_id, workspace_id)
        if project is None:
            return []
        return self._prompt_set_repo.list_by_project(project.id)

    def list_prompts_in_set(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, prompt_set_id: uuid.UUID
    ) -> list[Prompt]:
        """List prompts in a specific prompt set, ordered deterministically."""
        project = self._project_repo.get_in_workspace(project_id, workspace_id)
        if project is None:
            return []
        prompt_set = self._prompt_set_repo.get_by_id(prompt_set_id)
        if prompt_set is None or prompt_set.project_id != project.id:
            return []
        return self._prompt_repo.list_by_prompt_set(prompt_set_id)

    def is_stale(self, prompt_set: PromptSet, project: Project) -> bool:
        """Check if a prompt set is stale relative to the project."""
        return prompt_set.input_revision != project.prompt_input_revision

    def _create_prompt_set(
        self,
        project: Project,
        keywords: list[ProjectKeyword],
        competitors: list[Competitor],
        version: int,
        created_by_user_id: uuid.UUID | None,
    ) -> PromptSet:
        """Create a new PromptSet with generated prompts (not committed)."""
        # Generate prompt specs.
        specs = self._generation_service.generate_prompts(
            project=project,
            keywords=keywords,
            competitors=competitors,
        )

        # Create PromptSet record.
        prompt_set = PromptSet(
            project_id=project.id,
            version=version,
            input_revision=project.prompt_input_revision,
            status=PromptSetStatus.ACTIVE,
            generator_key=GENERATOR_KEY,
            created_by_user_id=created_by_user_id,
            activated_at=datetime.now(UTC),
        )
        self._prompt_set_repo.create(prompt_set)

        # Create Prompt records.
        prompts = self._generation_service.specs_to_models(specs, prompt_set.id)
        self._prompt_repo.create_batch(prompts)

        logger.info(
            "prompt_set_created",
            project_id=str(project.id),
            version=version,
            prompt_count=len(prompts),
            generator_key=GENERATOR_KEY,
        )

        return prompt_set

    def _load_active_keywords(self, project_id: uuid.UUID) -> list[ProjectKeyword]:
        """Load active keywords for a project."""
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

    def _load_active_competitors(self, project_id: uuid.UUID) -> list[Competitor]:
        """Load active competitors for a project."""
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
