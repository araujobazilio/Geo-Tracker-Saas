"""Project service — CRUD, status changes, and project summaries.

Handles:
  - List/get projects (tenant-scoped)
  - Update project configuration (with prompt_input_revision tracking)
  - Status transitions (pause, activate, archive)
  - Project summaries (keyword/competitor/provider counts, prompt set info, scan estimate)

Concurrency: status transitions that affect capacity (activate from
ARCHIVED) lock the Workspace row to re-check max_projects.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.enums import ProjectStatus
from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.core.normalization import (
    normalize_brand_aliases,
    normalize_country,
    normalize_domain,
    normalize_language,
    normalize_text_for_comparison,
)
from app.models.project import Project
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.keyword_repository import ProjectKeywordRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.tracking_repository import (
    ProjectProviderRepository,
    PromptRepository,
    PromptSetRepository,
)
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.entitlement_service import EntitlementService

logger = get_logger("app.project")


@dataclass
class ProjectUpdateInput:
    """Input for project update."""

    name: str | None = None
    domain: str | None = None
    brand_name: str | None = None
    brand_aliases: list[str] | None = None
    industry: str | None = None
    target_country: str | None = None
    target_language: str | None = None
    target_audience: str | None = None


@dataclass
class ProjectSummary:
    """Summary of a project for API responses."""

    project: Project
    keyword_count: int
    competitor_count: int
    enabled_provider_count: int
    current_prompt_set_version: int | None
    current_prompt_set_input_revision: int | None
    project_prompt_input_revision: int
    is_prompt_set_stale: bool
    standard_scan_ai_checks_estimate: int


class ProjectService:
    """CRUD and status management for projects."""

    # Fields that affect prompt generation and increment prompt_input_revision.
    _PROMPT_AFFECTING_FIELDS = frozenset(
        {
            "domain",
            "brand_name",
            "brand_aliases",
            "industry",
            "target_country",
            "target_language",
            "target_audience",
        }
    )

    def __init__(
        self,
        session: Session,
        entitlement_service: EntitlementService | None = None,
    ) -> None:
        self._session = session
        self._project_repo = ProjectRepository(session)
        self._keyword_repo = ProjectKeywordRepository(session)
        self._competitor_repo = CompetitorRepository(session)
        self._provider_repo = ProjectProviderRepository(session)
        self._prompt_set_repo = PromptSetRepository(session)
        self._prompt_repo = PromptRepository(session)
        self._workspace_repo = WorkspaceRepository(session)
        self._entitlement_service = entitlement_service or EntitlementService(session)

    def list_projects(self, workspace_id: uuid.UUID) -> list[Project]:
        """List all projects in a workspace."""
        return self._project_repo.list_by_workspace(workspace_id)

    def get_project(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> Project:
        """Get a project by ID (tenant-scoped).

        Raises ConflictError if not found.
        """
        project = self._project_repo.get_in_workspace(project_id, workspace_id)
        if project is None:
            raise ConflictError("Project not found.")
        return project

    def get_project_summary(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> ProjectSummary:
        """Get a project with summary information."""
        project = self.get_project(workspace_id, project_id)
        keyword_count = self._keyword_repo.count_active_by_project(project.id)
        competitor_count = self._competitor_repo.count_active_by_project(project.id)
        enabled_provider_count = self._provider_repo.count_enabled_by_project(project.id)

        current_set = self._prompt_set_repo.get_active_by_project(project.id)
        current_version = current_set.version if current_set else None
        current_input_rev = current_set.input_revision if current_set else None

        is_stale = (
            current_set is not None and current_set.input_revision != project.prompt_input_revision
        )

        # Standard scan estimate = current_prompt_count * enabled_provider_count
        prompt_count = self._prompt_repo.count_by_prompt_set(current_set.id) if current_set else 0
        scan_estimate = prompt_count * enabled_provider_count

        return ProjectSummary(
            project=project,
            keyword_count=keyword_count,
            competitor_count=competitor_count,
            enabled_provider_count=enabled_provider_count,
            current_prompt_set_version=current_version,
            current_prompt_set_input_revision=current_input_rev,
            project_prompt_input_revision=project.prompt_input_revision,
            is_prompt_set_stale=is_stale,
            standard_scan_ai_checks_estimate=scan_estimate,
        )

    def update_project(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        update: ProjectUpdateInput,
    ) -> Project:
        """Update project configuration.

        If a prompt-affecting field changes (normalized value differs),
        increments prompt_input_revision. Does NOT regenerate prompts.

        Raises ConflictError if project not found.
        """
        try:
            project = self._project_repo.get_in_workspace_for_update(project_id, workspace_id)
            if project is None:
                raise ConflictError("Project not found.")

            revision_changed = False

            if update.name is not None:
                if not update.name.strip():
                    raise ValidationError("Project name must not be empty.")
                project.name = update.name.strip()

            if update.domain is not None:
                new_domain = normalize_domain(update.domain)
                if new_domain != project.domain:
                    project.domain = new_domain
                    revision_changed = True

            if update.brand_name is not None:
                if not update.brand_name.strip():
                    raise ValidationError("Brand name must not be empty.")
                new_brand = update.brand_name.strip()
                if normalize_text_for_comparison(new_brand) != normalize_text_for_comparison(
                    project.brand_name
                ):
                    project.brand_name = new_brand
                    revision_changed = True

            if update.brand_aliases is not None:
                new_aliases = normalize_brand_aliases(update.brand_aliases)
                # Compare case-insensitively.
                old_set = {normalize_text_for_comparison(a) for a in project.brand_aliases}
                new_set = {normalize_text_for_comparison(a) for a in new_aliases}
                if old_set != new_set:
                    project.brand_aliases = new_aliases
                    revision_changed = True

            if update.industry is not None:
                new_industry = update.industry.strip() if update.industry else None
                if new_industry != project.industry:
                    project.industry = new_industry
                    revision_changed = True

            if update.target_country is not None:
                new_country = normalize_country(update.target_country)
                if new_country != project.target_country:
                    project.target_country = new_country
                    revision_changed = True

            if update.target_language is not None:
                new_lang = normalize_language(update.target_language)
                if new_lang != project.target_language:
                    project.target_language = new_lang
                    revision_changed = True

            if update.target_audience is not None:
                new_audience = update.target_audience.strip() if update.target_audience else None
                if new_audience != project.target_audience:
                    project.target_audience = new_audience
                    revision_changed = True

            if revision_changed:
                project.prompt_input_revision += 1

            self._session.commit()
            return project

        except (ConflictError, ValidationError):
            self._session.rollback()
            raise

    def pause_project(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> Project:
        """Pause an active project."""
        try:
            project = self._project_repo.get_in_workspace_for_update(project_id, workspace_id)
            if project is None:
                raise ConflictError("Project not found.")

            if project.status == ProjectStatus.PAUSED:
                self._session.commit()
                return project

            if project.status != ProjectStatus.ACTIVE:
                raise ConflictError(f"Cannot pause project with status {project.status}.")

            project.status = ProjectStatus.PAUSED
            self._session.commit()
            return project
        except ConflictError:
            self._session.rollback()
            raise

    def activate_project(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> Project:
        """Activate a paused or archived project.

        If activating from ARCHIVED, re-checks max_projects
        concurrency-safely by locking the Workspace row.
        """
        try:
            project = self._project_repo.get_in_workspace_for_update(project_id, workspace_id)
            if project is None:
                raise ConflictError("Project not found.")

            if project.status == ProjectStatus.ACTIVE:
                self._session.commit()
                return project

            if project.status not in (
                ProjectStatus.PAUSED,
                ProjectStatus.ARCHIVED,
            ):
                raise ConflictError(f"Cannot activate project with status {project.status}.")

            # If activating from ARCHIVED, re-check capacity.
            if project.status == ProjectStatus.ARCHIVED:
                # Lock workspace for capacity check.
                self._workspace_repo.get_for_update(workspace_id)
                current_count = self._project_repo.count_tracked_by_workspace(workspace_id)
                # The archived project doesn't count, so current_count
                # doesn't include it. Check if adding it would exceed.
                self._entitlement_service.require_project_capacity(workspace_id, current_count)

            project.status = ProjectStatus.ACTIVE
            self._session.commit()
            return project
        except (ConflictError, Exception):
            self._session.rollback()
            raise

    def archive_project(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> Project:
        """Archive a project (frees capacity, no hard delete)."""
        try:
            project = self._project_repo.get_in_workspace_for_update(project_id, workspace_id)
            if project is None:
                raise ConflictError("Project not found.")

            if project.status == ProjectStatus.ARCHIVED:
                self._session.commit()
                return project

            project.status = ProjectStatus.ARCHIVED
            self._session.commit()
            return project
        except ConflictError:
            self._session.rollback()
            raise
