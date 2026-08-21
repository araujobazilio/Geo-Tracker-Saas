"""Keyword and Competitor services — tracking management with capacity enforcement."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.enums import FunnelStage
from app.core.exceptions import ConflictError, QuotaExceededError, ValidationError
from app.core.logging import get_logger
from app.core.normalization import normalize_brand_aliases, normalize_domain, normalize_keyword
from app.models.tracking import Competitor, ProjectKeyword
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.keyword_repository import ProjectKeywordRepository
from app.repositories.project_repository import ProjectRepository
from app.services.entitlement_service import EntitlementService

logger = get_logger("app.tracking")


@dataclass
class KeywordCreateInput:
    text: str
    intent: str | None = None
    funnel_stage: FunnelStage | None = None


@dataclass
class KeywordUpdateInput:
    intent: str | None = None
    funnel_stage: FunnelStage | None = None
    active: bool | None = None


@dataclass
class CompetitorCreateInput:
    name: str
    domain: str
    aliases: list[str] | None = None


@dataclass
class CompetitorUpdateInput:
    name: str | None = None
    aliases: list[str] | None = None
    active: bool | None = None


class KeywordService:
    """Keyword management with normalization, capacity, and revision tracking."""

    def __init__(
        self,
        session: Session,
        entitlement_service: EntitlementService | None = None,
    ) -> None:
        self._session = session
        self._project_repo = ProjectRepository(session)
        self._keyword_repo = ProjectKeywordRepository(session)
        self._entitlement_service = entitlement_service or EntitlementService(session)

    def list_keywords(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> list[ProjectKeyword]:
        """List all keywords in a project (tenant-scoped)."""
        project = self._project_repo.get_in_workspace(project_id, workspace_id)
        if project is None:
            raise ConflictError("Project not found.")
        return self._keyword_repo.list_by_project(project.id)

    def add_keyword(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        input: KeywordCreateInput,
    ) -> ProjectKeyword:
        """Add a keyword to a project.

        Locks the project row for concurrency-safe capacity check.
        Increments prompt_input_revision.
        """
        try:
            display, normalized = normalize_keyword(input.text)

            project = self._project_repo.get_in_workspace_for_update(project_id, workspace_id)
            if project is None:
                raise ConflictError("Project not found.")

            # Check for duplicate.
            existing = self._keyword_repo.get_by_normalized_text(project.id, normalized)
            if existing is not None:
                raise ConflictError(f"Keyword already exists: {display}")

            # Check capacity.
            current_count = self._keyword_repo.count_active_by_project(project.id)
            self._entitlement_service.require_keyword_capacity(
                workspace_id, project.id, current_count
            )

            keyword = ProjectKeyword(
                project_id=project.id,
                text=display,
                normalized_text=normalized,
                intent=input.intent,
                funnel_stage=input.funnel_stage,
                active=True,
            )
            self._keyword_repo.create(keyword)

            # Increment revision (keyword set changed).
            project.prompt_input_revision += 1

            self._session.commit()
            return keyword

        except (ConflictError, QuotaExceededError, ValidationError):
            self._session.rollback()
            raise

    def add_keywords_bulk(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        inputs: list[KeywordCreateInput],
    ) -> list[ProjectKeyword]:
        """Add multiple keywords atomically.

        All keywords are validated and added in a single transaction.
        If any fails, none are added.
        """
        try:
            project = self._project_repo.get_in_workspace_for_update(project_id, workspace_id)
            if project is None:
                raise ConflictError("Project not found.")

            # Normalize and validate all keywords.
            seen: set[str] = set()
            normalized_inputs: list[tuple[str, str, str | None, str | None]] = []
            for inp in inputs:
                display, normalized = normalize_keyword(inp.text)
                if normalized in seen:
                    raise ConflictError(f"Duplicate keyword in request: {display}")
                existing = self._keyword_repo.get_by_normalized_text(project.id, normalized)
                if existing is not None:
                    raise ConflictError(f"Keyword already exists: {display}")
                seen.add(normalized)
                normalized_inputs.append((display, normalized, inp.intent, inp.funnel_stage))

            # Check capacity for all new keywords.
            current_count = self._keyword_repo.count_active_by_project(project.id)
            new_count = len(normalized_inputs)
            ent = self._entitlement_service.get_effective_entitlements(workspace_id)
            if current_count + new_count > ent.max_keywords_per_project:
                raise QuotaExceededError(
                    f"Keyword limit reached ({ent.max_keywords_per_project}) for this project."
                )

            # Create all keywords.
            created: list[ProjectKeyword] = []
            for display, normalized, intent, funnel in normalized_inputs:
                keyword = ProjectKeyword(
                    project_id=project.id,
                    text=display,
                    normalized_text=normalized,
                    intent=intent,
                    funnel_stage=funnel,
                    active=True,
                )
                self._keyword_repo.create(keyword)
                created.append(keyword)

            # Increment revision.
            project.prompt_input_revision += 1

            self._session.commit()
            return created

        except (ConflictError, QuotaExceededError, ValidationError):
            self._session.rollback()
            raise

    def update_keyword(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        keyword_id: uuid.UUID,
        update: KeywordUpdateInput,
    ) -> ProjectKeyword:
        """Update a keyword's intent, funnel_stage, or active status.

        Keyword text is immutable. Changes to intent/funnel_stage/active
        increment prompt_input_revision.
        """
        try:
            project = self._project_repo.get_in_workspace_for_update(project_id, workspace_id)
            if project is None:
                raise ConflictError("Project not found.")

            keyword = self._keyword_repo.get_in_project_for_update(keyword_id, project.id)
            if keyword is None:
                raise ConflictError("Keyword not found.")

            revision_changed = False

            if update.intent is not None and update.intent != keyword.intent:
                keyword.intent = update.intent
                revision_changed = True

            if update.funnel_stage is not None and update.funnel_stage != keyword.funnel_stage:
                keyword.funnel_stage = update.funnel_stage
                revision_changed = True

            if update.active is not None and update.active != keyword.active:
                # If reactivating, check capacity.
                if update.active and not keyword.active:
                    current_count = self._keyword_repo.count_active_by_project(project.id)
                    self._entitlement_service.require_keyword_capacity(
                        workspace_id, project.id, current_count
                    )
                keyword.active = update.active
                revision_changed = True

            if revision_changed:
                project.prompt_input_revision += 1

            self._session.commit()
            return keyword

        except (ConflictError, QuotaExceededError):
            self._session.rollback()
            raise


class CompetitorService:
    """Competitor management with domain normalization and capacity enforcement."""

    def __init__(
        self,
        session: Session,
        entitlement_service: EntitlementService | None = None,
    ) -> None:
        self._session = session
        self._project_repo = ProjectRepository(session)
        self._competitor_repo = CompetitorRepository(session)
        self._entitlement_service = entitlement_service or EntitlementService(session)

    def list_competitors(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> list[Competitor]:
        """List all competitors in a project (tenant-scoped)."""
        project = self._project_repo.get_in_workspace(project_id, workspace_id)
        if project is None:
            raise ConflictError("Project not found.")
        return self._competitor_repo.list_by_project(project.id)

    def add_competitor(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        input: CompetitorCreateInput,
    ) -> Competitor:
        """Add a competitor to a project.

        Domain is normalized and must not match the project domain.
        Competitor domain uniqueness is per project.
        Increments prompt_input_revision.
        """
        try:
            if not input.name or not input.name.strip():
                raise ValidationError("Competitor name is required.")

            domain = normalize_domain(input.domain)
            aliases = normalize_brand_aliases(input.aliases or [])

            project = self._project_repo.get_in_workspace_for_update(project_id, workspace_id)
            if project is None:
                raise ConflictError("Project not found.")

            # Reject if domain matches project domain.
            if domain == project.domain:
                raise ValidationError(
                    f"Competitor domain '{domain}' cannot match the project domain."
                )

            # Check for duplicate.
            existing = self._competitor_repo.get_by_domain(project.id, domain)
            if existing is not None:
                raise ConflictError(f"Competitor with domain '{domain}' already exists.")

            # Check capacity.
            current_count = self._competitor_repo.count_active_by_project(project.id)
            self._entitlement_service.require_competitor_capacity(
                workspace_id, project.id, current_count
            )

            competitor = Competitor(
                project_id=project.id,
                name=input.name.strip(),
                domain=domain,
                aliases=aliases,
                active=True,
            )
            self._competitor_repo.create(competitor)

            # Increment revision (competitor set changed).
            project.prompt_input_revision += 1

            self._session.commit()
            return competitor

        except (ConflictError, QuotaExceededError, ValidationError):
            self._session.rollback()
            raise

    def update_competitor(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        competitor_id: uuid.UUID,
        update: CompetitorUpdateInput,
    ) -> Competitor:
        """Update a competitor's name, aliases, or active status.

        Domain is immutable. Changes increment prompt_input_revision.
        """
        try:
            project = self._project_repo.get_in_workspace_for_update(project_id, workspace_id)
            if project is None:
                raise ConflictError("Project not found.")

            competitor = self._competitor_repo.get_in_project_for_update(competitor_id, project.id)
            if competitor is None:
                raise ConflictError("Competitor not found.")

            revision_changed = False

            if update.name is not None:
                if not update.name.strip():
                    raise ValidationError("Competitor name must not be empty.")
                competitor.name = update.name.strip()

            if update.aliases is not None:
                competitor.aliases = normalize_brand_aliases(update.aliases)
                revision_changed = True

            if update.active is not None and update.active != competitor.active:
                if update.active and not competitor.active:
                    current_count = self._competitor_repo.count_active_by_project(project.id)
                    self._entitlement_service.require_competitor_capacity(
                        workspace_id, project.id, current_count
                    )
                competitor.active = update.active
                revision_changed = True

            if revision_changed:
                project.prompt_input_revision += 1

            self._session.commit()
            return competitor

        except (ConflictError, QuotaExceededError, ValidationError):
            self._session.rollback()
            raise
