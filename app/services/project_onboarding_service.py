"""Project onboarding service — atomic project creation with initial prompt set.

Onboarding is atomic: if any step fails, the entire operation rolls back.
No partial project, no orphan keywords, no half-created prompt set.

Steps:
  1. Lock Workspace row (FOR UPDATE)
  2. Check project entitlement capacity
  3. Create Project (with normalized domain, brand, market config)
  4. Validate and add Keywords (with normalization)
  5. Validate and add Competitors (with domain normalization)
  6. Validate and add ProjectProviders (entitlement-checked)
  7. Generate initial PromptSet v1
  8. Commit

Concurrency: the Workspace lock prevents two concurrent onboarding
requests from exceeding max_projects.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.enums import LLMProvider, ProjectStatus
from app.core.exceptions import ConflictError, QuotaExceededError, ValidationError
from app.core.logging import get_logger
from app.core.normalization import (
    normalize_brand_aliases,
    normalize_country,
    normalize_domain,
    normalize_keyword,
    normalize_language,
)
from app.models.project import Project
from app.models.tracking import Competitor, ProjectKeyword, ProjectProvider
from app.repositories.competitor_repository import CompetitorRepository
from app.repositories.keyword_repository import ProjectKeywordRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.tracking_repository import ProjectProviderRepository
from app.repositories.workspace_repository import WorkspaceRepository
from app.services.entitlement_service import EntitlementService
from app.services.prompt_set_service import PromptSetService

logger = get_logger("app.project_onboarding")


@dataclass
class KeywordInput:
    """Input for a single keyword during onboarding."""

    text: str
    intent: str | None = None
    funnel_stage: str | None = None


@dataclass
class CompetitorInput:
    """Input for a single competitor during onboarding."""

    name: str
    domain: str
    aliases: list[str] = field(default_factory=list)


@dataclass
class OnboardingRequest:
    """Complete onboarding request payload."""

    name: str
    domain: str
    brand_name: str
    brand_aliases: list[str] = field(default_factory=list)
    industry: str | None = None
    target_country: str | None = None
    target_language: str | None = None
    target_audience: str | None = None
    keywords: list[KeywordInput] = field(default_factory=list)
    competitors: list[CompetitorInput] = field(default_factory=list)
    providers: list[LLMProvider] = field(default_factory=list)


class ProjectOnboardingService:
    """Atomic project onboarding with initial prompt set generation."""

    def __init__(
        self,
        session: Session,
        entitlement_service: EntitlementService | None = None,
        prompt_set_service: PromptSetService | None = None,
    ) -> None:
        self._session = session
        self._workspace_repo = WorkspaceRepository(session)
        self._project_repo = ProjectRepository(session)
        self._keyword_repo = ProjectKeywordRepository(session)
        self._competitor_repo = CompetitorRepository(session)
        self._provider_repo = ProjectProviderRepository(session)
        self._entitlement_service = entitlement_service or EntitlementService(session)
        self._prompt_set_service = prompt_set_service or PromptSetService(session)

    def onboard_project(
        self,
        workspace_id: uuid.UUID,
        request: OnboardingRequest,
        created_by_user_id: uuid.UUID | None = None,
    ) -> Project:
        """Atomically create a project with keywords, competitors, providers, and initial prompt set.

        Raises:
            ValidationError: if input validation fails.
            QuotaExceededError: if project/keyword/competitor capacity exceeded.
            ConflictError: if domain conflicts or provider not allowed.
        """
        try:
            # Validate and normalize inputs BEFORE locking.
            normalized = self._validate_and_normalize(request)

            # 1. Lock Workspace row.
            workspace = self._workspace_repo.get_for_update(workspace_id)
            if workspace is None:
                raise ConflictError("Workspace not found.")

            # 2. Check project capacity.
            current_count = self._project_repo.count_tracked_by_workspace(workspace_id)
            self._entitlement_service.require_project_capacity(workspace_id, current_count)

            # 3. Create Project.
            project = Project(
                workspace_id=workspace_id,
                name=normalized.name,
                domain=normalized.domain,
                brand_name=normalized.brand_name,
                brand_aliases=normalized.brand_aliases,
                industry=normalized.industry,
                target_country=normalized.target_country,
                target_language=normalized.target_language,
                target_audience=normalized.target_audience,
                status=ProjectStatus.ACTIVE,
                prompt_input_revision=1,
            )
            self._project_repo.create(project)

            # 4. Add Keywords.
            if not normalized.keywords:
                raise ValidationError("At least one keyword is required.")

            seen_normalized: set[str] = set()
            for kw_input in normalized.keywords:
                if kw_input.normalized_text in seen_normalized:
                    raise ValidationError(f"Duplicate keyword: {kw_input.text}")
                seen_normalized.add(kw_input.normalized_text)

                keyword = ProjectKeyword(
                    project_id=project.id,
                    text=kw_input.text,
                    normalized_text=kw_input.normalized_text,
                    intent=kw_input.intent,
                    funnel_stage=kw_input.funnel_stage,
                    active=True,
                )
                self._keyword_repo.create(keyword)

            # Check keyword capacity.
            keyword_count = self._keyword_repo.count_active_by_project(project.id)
            self._entitlement_service.require_keyword_capacity(
                workspace_id, project.id, keyword_count
            )

            # 5. Add Competitors.
            seen_domains: set[str] = set()
            for comp_input in normalized.competitors:
                if comp_input.domain == normalized.domain:
                    raise ValidationError(
                        f"Competitor domain '{comp_input.domain}' cannot "
                        f"match the project domain."
                    )
                if comp_input.domain in seen_domains:
                    raise ValidationError(f"Duplicate competitor domain: {comp_input.domain}")
                seen_domains.add(comp_input.domain)

                competitor = Competitor(
                    project_id=project.id,
                    name=comp_input.name,
                    domain=comp_input.domain,
                    aliases=comp_input.aliases,
                    active=True,
                )
                self._competitor_repo.create(competitor)

            # Check competitor capacity.
            competitor_count = self._competitor_repo.count_active_by_project(project.id)
            self._entitlement_service.require_competitor_capacity(
                workspace_id, project.id, competitor_count
            )

            # 6. Add Providers.
            if not normalized.providers:
                raise ValidationError("At least one enabled provider is required.")

            for provider in normalized.providers:
                self._entitlement_service.require_provider(workspace_id, provider)
                pp = ProjectProvider(
                    project_id=project.id,
                    provider=provider,
                    enabled=True,
                )
                self._provider_repo.create(pp)

            # 7. Generate initial PromptSet v1.
            keywords = self._keyword_repo.list_active_by_project(project.id)
            competitors = self._competitor_repo.list_active_by_project(project.id)
            self._prompt_set_service.generate_initial_prompt_set(
                project=project,
                keywords=keywords,
                competitors=competitors,
                created_by_user_id=created_by_user_id,
            )

            # 8. Commit.
            self._session.commit()

            logger.info(
                "project_onboarded",
                workspace_id=str(workspace_id),
                project_id=str(project.id),
                keyword_count=len(normalized.keywords),
                competitor_count=len(normalized.competitors),
                provider_count=len(normalized.providers),
            )

            return project

        except (ValidationError, QuotaExceededError, ConflictError):
            self._session.rollback()
            raise
        except Exception:
            self._session.rollback()
            raise

    def _validate_and_normalize(self, request: OnboardingRequest) -> _NormalizedOnboarding:
        """Validate and normalize all onboarding inputs."""
        if not request.name or not request.name.strip():
            raise ValidationError("Project name is required.")
        if len(request.name.strip()) > 255:
            raise ValidationError("Project name too long (max 255 characters).")

        if not request.brand_name or not request.brand_name.strip():
            raise ValidationError("Brand name is required.")
        if len(request.brand_name.strip()) > 255:
            raise ValidationError("Brand name too long (max 255 characters).")

        domain = normalize_domain(request.domain)
        brand_aliases = normalize_brand_aliases(request.brand_aliases)
        target_country = normalize_country(request.target_country)
        target_language = normalize_language(request.target_language)

        if request.industry and len(request.industry.strip()) > 255:
            raise ValidationError("Industry too long (max 255 characters).")

        if request.target_audience and len(request.target_audience.strip()) > 255:
            raise ValidationError("Target audience too long (max 255 characters).")

        # Normalize keywords.
        normalized_keywords: list[_NormalizedKeyword] = []
        for kw in request.keywords:
            display, norm = normalize_keyword(kw.text)
            normalized_keywords.append(
                _NormalizedKeyword(
                    text=display,
                    normalized_text=norm,
                    intent=kw.intent,
                    funnel_stage=kw.funnel_stage,
                )
            )

        # Normalize competitors.
        normalized_competitors: list[_NormalizedCompetitor] = []
        for comp in request.competitors:
            if not comp.name or not comp.name.strip():
                raise ValidationError("Competitor name is required.")
            comp_domain = normalize_domain(comp.domain)
            comp_aliases = normalize_brand_aliases(comp.aliases)
            normalized_competitors.append(
                _NormalizedCompetitor(
                    name=comp.name.strip(),
                    domain=comp_domain,
                    aliases=comp_aliases,
                )
            )

        return _NormalizedOnboarding(
            name=request.name.strip(),
            domain=domain,
            brand_name=request.brand_name.strip(),
            brand_aliases=brand_aliases,
            industry=request.industry.strip() if request.industry else None,
            target_country=target_country,
            target_language=target_language,
            target_audience=(request.target_audience.strip() if request.target_audience else None),
            keywords=normalized_keywords,
            competitors=normalized_competitors,
            providers=list(request.providers),
        )


@dataclass
class _NormalizedKeyword:
    text: str
    normalized_text: str
    intent: str | None
    funnel_stage: str | None


@dataclass
class _NormalizedCompetitor:
    name: str
    domain: str
    aliases: list[str]


@dataclass
class _NormalizedOnboarding:
    name: str
    domain: str
    brand_name: str
    brand_aliases: list[str]
    industry: str | None
    target_country: str | None
    target_language: str | None
    target_audience: str | None
    keywords: list[_NormalizedKeyword]
    competitors: list[_NormalizedCompetitor]
    providers: list[LLMProvider]
