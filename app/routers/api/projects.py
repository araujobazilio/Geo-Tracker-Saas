"""Projects API router.

Endpoints:
  POST   /api/v1/workspaces/{ws}/projects                          — onboard project
  GET    /api/v1/workspaces/{ws}/projects                          — list projects
  GET    /api/v1/workspaces/{ws}/projects/{pid}                    — get project summary
  PATCH  /api/v1/workspaces/{ws}/projects/{pid}                    — update project
  POST   /api/v1/workspaces/{ws}/projects/{pid}/pause              — pause project
  POST   /api/v1/workspaces/{ws}/projects/{pid}/activate           — activate project
  POST   /api/v1/workspaces/{ws}/projects/{pid}/archive            — archive project

  GET    /api/v1/workspaces/{ws}/projects/{pid}/keywords           — list keywords
  POST   /api/v1/workspaces/{ws}/projects/{pid}/keywords           — add keyword
  PATCH  /api/v1/workspaces/{ws}/projects/{pid}/keywords/{kid}     — update keyword

  GET    /api/v1/workspaces/{ws}/projects/{pid}/competitors        — list competitors
  POST   /api/v1/workspaces/{ws}/projects/{pid}/competitors        — add competitor
  PATCH  /api/v1/workspaces/{ws}/projects/{pid}/competitors/{cid}  — update competitor

  GET    /api/v1/workspaces/{ws}/projects/{pid}/providers          — list providers
  PUT    /api/v1/workspaces/{ws}/projects/{pid}/providers          — set providers

  GET    /api/v1/workspaces/{ws}/projects/{pid}/prompt-sets        — list prompt sets
  GET    /api/v1/workspaces/{ws}/projects/{pid}/prompt-sets/current — get current set
  GET    /api/v1/workspaces/{ws}/projects/{pid}/prompt-sets/{version} — get set by version
  POST   /api/v1/workspaces/{ws}/projects/{pid}/prompt-sets/regenerate — regenerate
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.enums import WorkspaceRole
from app.dependencies import (
    get_audit_service,
    get_entitlement_service,
    get_workspace_auth_service,
    require_authenticated_user,
)
from app.models.user import User
from app.schemas.projects import (
    CompetitorCreateRequest,
    CompetitorResponse,
    CompetitorUpdateRequest,
    KeywordCreateRequest,
    KeywordResponse,
    KeywordUpdateRequest,
    ProjectCreateRequest,
    ProjectResponse,
    ProjectSummaryResponse,
    ProjectUpdateRequest,
    PromptResponse,
    PromptSetDetailResponse,
    PromptSetSummaryResponse,
    ProviderResponse,
    ProviderUpdateRequest,
)
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService
from app.services.project_onboarding_service import (
    CompetitorInput,
    KeywordInput,
    OnboardingRequest,
    ProjectOnboardingService,
)
from app.services.project_provider_service import ProjectProviderService
from app.services.project_service import ProjectService, ProjectUpdateInput
from app.services.prompt_set_service import PromptSetService
from app.services.tracking_service import (
    CompetitorCreateInput,
    CompetitorService,
    CompetitorUpdateInput,
    KeywordCreateInput,
    KeywordService,
    KeywordUpdateInput,
)
from app.services.workspace_auth_service import WorkspaceAuthorizationService

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/projects", tags=["projects"])


# --- Dependency factories ---


def get_onboarding_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> ProjectOnboardingService:
    return ProjectOnboardingService(
        session=entitlement_service._session,
        entitlement_service=entitlement_service,
    )


def get_project_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> ProjectService:
    return ProjectService(
        session=entitlement_service._session,
        entitlement_service=entitlement_service,
    )


def get_keyword_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> KeywordService:
    return KeywordService(
        session=entitlement_service._session,
        entitlement_service=entitlement_service,
    )


def get_competitor_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> CompetitorService:
    return CompetitorService(
        session=entitlement_service._session,
        entitlement_service=entitlement_service,
    )


def get_project_provider_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> ProjectProviderService:
    return ProjectProviderService(
        session=entitlement_service._session,
        entitlement_service=entitlement_service,
    )


def get_prompt_set_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> PromptSetService:
    return PromptSetService(session=entitlement_service._session)


# --- Project endpoints ---


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    workspace_id: uuid.UUID,
    request: ProjectCreateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    onboarding_service: Annotated[ProjectOnboardingService, Depends(get_onboarding_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> ProjectResponse:
    """Onboard a new project with keywords, competitors, providers, and initial prompt set.

    Requires OWNER or ADMIN role.
    """
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    onboarding_request = OnboardingRequest(
        name=request.name,
        domain=request.domain,
        brand_name=request.brand_name,
        brand_aliases=request.brand_aliases,
        industry=request.industry,
        target_country=request.target_country,
        target_language=request.target_language,
        target_audience=request.target_audience,
        keywords=[
            KeywordInput(text=kw.text, intent=kw.intent, funnel_stage=kw.funnel_stage)
            for kw in request.keywords
        ],
        competitors=[
            CompetitorInput(name=c.name, domain=c.domain, aliases=c.aliases)
            for c in request.competitors
        ],
        providers=list(request.providers),
    )
    project = onboarding_service.onboard_project(
        workspace_id=workspace_id,
        request=onboarding_request,
        created_by_user_id=user.id,
    )
    audit.record(
        action="PROJECT_CREATED",
        workspace_id=workspace_id,
        user_id=user.id,
        entity_type="project",
        entity_id=project.id,
    )
    return ProjectResponse.model_validate(project)


@router.get("", response_model=list[ProjectResponse])
def list_projects(
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> list[ProjectResponse]:
    """List all projects in a workspace. Requires membership."""
    auth_service.require_membership(workspace_id, user.id)
    projects = project_service.list_projects(workspace_id)
    return [ProjectResponse.model_validate(p) for p in projects]


@router.get("/{project_id}", response_model=ProjectSummaryResponse)
def get_project(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> ProjectSummaryResponse:
    """Get a project with summary information. Requires membership."""
    auth_service.require_membership(workspace_id, user.id)
    summary = project_service.get_project_summary(workspace_id, project_id)
    return ProjectSummaryResponse(
        project=ProjectResponse.model_validate(summary.project),
        keyword_count=summary.keyword_count,
        competitor_count=summary.competitor_count,
        enabled_provider_count=summary.enabled_provider_count,
        current_prompt_set_version=summary.current_prompt_set_version,
        current_prompt_set_input_revision=summary.current_prompt_set_input_revision,
        project_prompt_input_revision=summary.project_prompt_input_revision,
        is_prompt_set_stale=summary.is_prompt_set_stale,
        standard_scan_ai_checks_estimate=summary.standard_scan_ai_checks_estimate,
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: ProjectUpdateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> ProjectResponse:
    """Update project configuration. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    update = ProjectUpdateInput(
        name=request.name,
        domain=request.domain,
        brand_name=request.brand_name,
        brand_aliases=request.brand_aliases,
        industry=request.industry,
        target_country=request.target_country,
        target_language=request.target_language,
        target_audience=request.target_audience,
    )
    project = project_service.update_project(workspace_id, project_id, update)
    audit.record(
        action="PROJECT_UPDATED",
        workspace_id=workspace_id,
        user_id=user.id,
        entity_type="project",
        entity_id=project.id,
    )
    return ProjectResponse.model_validate(project)


@router.post("/{project_id}/pause", response_model=ProjectResponse)
def pause_project(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> ProjectResponse:
    """Pause a project. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    project = project_service.pause_project(workspace_id, project_id)
    audit.record(
        action="PROJECT_PAUSED",
        workspace_id=workspace_id,
        user_id=user.id,
        entity_type="project",
        entity_id=project.id,
    )
    return ProjectResponse.model_validate(project)


@router.post("/{project_id}/activate", response_model=ProjectResponse)
def activate_project(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> ProjectResponse:
    """Activate a paused or archived project. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    project = project_service.activate_project(workspace_id, project_id)
    audit.record(
        action="PROJECT_ACTIVATED",
        workspace_id=workspace_id,
        user_id=user.id,
        entity_type="project",
        entity_id=project.id,
    )
    return ProjectResponse.model_validate(project)


@router.post("/{project_id}/archive", response_model=ProjectResponse)
def archive_project(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> ProjectResponse:
    """Archive a project (frees capacity, no hard delete). Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    project = project_service.archive_project(workspace_id, project_id)
    audit.record(
        action="PROJECT_ARCHIVED",
        workspace_id=workspace_id,
        user_id=user.id,
        entity_type="project",
        entity_id=project.id,
    )
    return ProjectResponse.model_validate(project)


# --- Keyword endpoints ---


@router.get("/{project_id}/keywords", response_model=list[KeywordResponse])
def list_keywords(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    keyword_service: Annotated[KeywordService, Depends(get_keyword_service)],
) -> list[KeywordResponse]:
    """List all keywords in a project. Requires membership."""
    auth_service.require_membership(workspace_id, user.id)
    keywords = keyword_service.list_keywords(workspace_id, project_id)
    return [KeywordResponse.model_validate(kw) for kw in keywords]


@router.post(
    "/{project_id}/keywords",
    response_model=KeywordResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_keyword(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: KeywordCreateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    keyword_service: Annotated[KeywordService, Depends(get_keyword_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> KeywordResponse:
    """Add a keyword to a project. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    input_obj = KeywordCreateInput(
        text=request.text, intent=request.intent, funnel_stage=request.funnel_stage
    )
    keyword = keyword_service.add_keyword(workspace_id, project_id, input_obj)
    audit.record(
        action="KEYWORD_ADDED",
        workspace_id=workspace_id,
        user_id=user.id,
        entity_type="project_keyword",
        entity_id=keyword.id,
    )
    return KeywordResponse.model_validate(keyword)


@router.patch("/{project_id}/keywords/{keyword_id}", response_model=KeywordResponse)
def update_keyword(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    keyword_id: uuid.UUID,
    request: KeywordUpdateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    keyword_service: Annotated[KeywordService, Depends(get_keyword_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> KeywordResponse:
    """Update a keyword's intent, funnel_stage, or active status. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    update = KeywordUpdateInput(
        intent=request.intent,
        funnel_stage=request.funnel_stage,
        active=request.active,
    )
    keyword = keyword_service.update_keyword(workspace_id, project_id, keyword_id, update)
    audit.record(
        action="KEYWORD_UPDATED",
        workspace_id=workspace_id,
        user_id=user.id,
        entity_type="project_keyword",
        entity_id=keyword.id,
    )
    return KeywordResponse.model_validate(keyword)


# --- Competitor endpoints ---


@router.get("/{project_id}/competitors", response_model=list[CompetitorResponse])
def list_competitors(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    competitor_service: Annotated[CompetitorService, Depends(get_competitor_service)],
) -> list[CompetitorResponse]:
    """List all competitors in a project. Requires membership."""
    auth_service.require_membership(workspace_id, user.id)
    competitors = competitor_service.list_competitors(workspace_id, project_id)
    return [CompetitorResponse.model_validate(c) for c in competitors]


@router.post(
    "/{project_id}/competitors",
    response_model=CompetitorResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_competitor(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: CompetitorCreateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    competitor_service: Annotated[CompetitorService, Depends(get_competitor_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> CompetitorResponse:
    """Add a competitor to a project. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    input_obj = CompetitorCreateInput(
        name=request.name, domain=request.domain, aliases=request.aliases
    )
    competitor = competitor_service.add_competitor(workspace_id, project_id, input_obj)
    audit.record(
        action="COMPETITOR_ADDED",
        workspace_id=workspace_id,
        user_id=user.id,
        entity_type="competitor",
        entity_id=competitor.id,
    )
    return CompetitorResponse.model_validate(competitor)


@router.patch(
    "/{project_id}/competitors/{competitor_id}",
    response_model=CompetitorResponse,
)
def update_competitor(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    competitor_id: uuid.UUID,
    request: CompetitorUpdateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    competitor_service: Annotated[CompetitorService, Depends(get_competitor_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> CompetitorResponse:
    """Update a competitor's name, aliases, or active status. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    update = CompetitorUpdateInput(
        name=request.name, aliases=request.aliases, active=request.active
    )
    competitor = competitor_service.update_competitor(
        workspace_id, project_id, competitor_id, update
    )
    audit.record(
        action="COMPETITOR_UPDATED",
        workspace_id=workspace_id,
        user_id=user.id,
        entity_type="competitor",
        entity_id=competitor.id,
    )
    return CompetitorResponse.model_validate(competitor)


# --- Provider endpoints ---


@router.get("/{project_id}/providers", response_model=list[ProviderResponse])
def list_providers(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    provider_service: Annotated[ProjectProviderService, Depends(get_project_provider_service)],
) -> list[ProviderResponse]:
    """List all configured providers with allowed_by_plan status. Requires membership."""
    auth_service.require_membership(workspace_id, user.id)
    providers = provider_service.list_providers(workspace_id, project_id)
    return [ProviderResponse.model_validate(p) for p in providers]


@router.put("/{project_id}/providers", response_model=list[ProviderResponse])
def set_providers(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: ProviderUpdateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    provider_service: Annotated[ProjectProviderService, Depends(get_project_provider_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> list[ProviderResponse]:
    """Set the enabled provider set for a project (PUT replace). Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    provider_service.set_providers(workspace_id, project_id, list(request.providers))
    audit.record(
        action="PROJECT_PROVIDERS_UPDATED",
        workspace_id=workspace_id,
        user_id=user.id,
        entity_type="project",
        entity_id=project_id,
    )
    # Return the updated list with allowed_by_plan info.
    providers = provider_service.list_providers(workspace_id, project_id)
    return [ProviderResponse.model_validate(p) for p in providers]


# --- Prompt set endpoints ---


@router.get("/{project_id}/prompt-sets", response_model=list[PromptSetSummaryResponse])
def list_prompt_sets(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    prompt_set_service: Annotated[PromptSetService, Depends(get_prompt_set_service)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> list[PromptSetSummaryResponse]:
    """List all prompt sets for a project. Requires membership."""
    auth_service.require_membership(workspace_id, user.id)
    project = project_service.get_project(workspace_id, project_id)
    sets = prompt_set_service.list_prompt_sets(workspace_id, project_id)
    return [
        PromptSetSummaryResponse(
            id=ps.id,
            project_id=ps.project_id,
            version=ps.version,
            input_revision=ps.input_revision,
            status=ps.status,
            generator_key=ps.generator_key,
            created_at=ps.created_at,
            activated_at=ps.activated_at,
            prompt_count=prompt_set_service._prompt_repo.count_by_prompt_set(ps.id),
            is_stale=ps.input_revision != project.prompt_input_revision,
        )
        for ps in sets
    ]


@router.get("/{project_id}/prompt-sets/current", response_model=PromptSetDetailResponse)
def get_current_prompt_set(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    prompt_set_service: Annotated[PromptSetService, Depends(get_prompt_set_service)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> PromptSetDetailResponse:
    """Get the current ACTIVE prompt set with prompts. Requires membership."""
    auth_service.require_membership(workspace_id, user.id)
    project = project_service.get_project(workspace_id, project_id)
    ps = prompt_set_service.get_current_prompt_set(workspace_id, project_id)
    if ps is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("No active prompt set found for this project.")
    prompts = prompt_set_service.list_prompts_in_set(workspace_id, project_id, ps.id)
    return PromptSetDetailResponse(
        id=ps.id,
        project_id=ps.project_id,
        version=ps.version,
        input_revision=ps.input_revision,
        status=ps.status,
        generator_key=ps.generator_key,
        created_at=ps.created_at,
        activated_at=ps.activated_at,
        prompt_count=len(prompts),
        is_stale=ps.input_revision != project.prompt_input_revision,
        prompts=[PromptResponse.model_validate(p) for p in prompts],
    )


@router.get("/{project_id}/prompt-sets/{version}", response_model=PromptSetDetailResponse)
def get_prompt_set_by_version(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    version: int,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    prompt_set_service: Annotated[PromptSetService, Depends(get_prompt_set_service)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
) -> PromptSetDetailResponse:
    """Get a specific prompt set version with prompts. Requires membership."""
    auth_service.require_membership(workspace_id, user.id)
    project = project_service.get_project(workspace_id, project_id)
    ps = prompt_set_service.get_prompt_set_by_version(workspace_id, project_id, version)
    if ps is None:
        from app.core.exceptions import NotFoundError

        raise NotFoundError(f"Prompt set version {version} not found.")
    prompts = prompt_set_service.list_prompts_in_set(workspace_id, project_id, ps.id)
    return PromptSetDetailResponse(
        id=ps.id,
        project_id=ps.project_id,
        version=ps.version,
        input_revision=ps.input_revision,
        status=ps.status,
        generator_key=ps.generator_key,
        created_at=ps.created_at,
        activated_at=ps.activated_at,
        prompt_count=len(prompts),
        is_stale=ps.input_revision != project.prompt_input_revision,
        prompts=[PromptResponse.model_validate(p) for p in prompts],
    )


@router.post("/{project_id}/prompt-sets/regenerate", response_model=PromptSetSummaryResponse)
def regenerate_prompt_set(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    prompt_set_service: Annotated[PromptSetService, Depends(get_prompt_set_service)],
    project_service: Annotated[ProjectService, Depends(get_project_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> PromptSetSummaryResponse:
    """Regenerate the prompt set for a project. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    ps = prompt_set_service.regenerate_prompt_set(
        workspace_id, project_id, created_by_user_id=user.id
    )
    project = project_service.get_project(workspace_id, project_id)
    audit.record(
        action="PROMPT_SET_CREATED",
        workspace_id=workspace_id,
        user_id=user.id,
        entity_type="prompt_set",
        entity_id=ps.id,
    )
    return PromptSetSummaryResponse(
        id=ps.id,
        project_id=ps.project_id,
        version=ps.version,
        input_revision=ps.input_revision,
        status=ps.status,
        generator_key=ps.generator_key,
        created_at=ps.created_at,
        activated_at=ps.activated_at,
        prompt_count=prompt_set_service._prompt_repo.count_by_prompt_set(ps.id),
        is_stale=ps.input_revision != project.prompt_input_revision,
    )
