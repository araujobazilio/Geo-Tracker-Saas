"""Web project configuration routes - prompt regeneration, brand/topics/competitors/providers.

Calls existing services (ProjectService, TrackingService, ProjectProviderService,
PromptSetService). 0 AI Checks for all operations.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import FunnelStage, LLMProvider, WorkspaceRole
from app.core.exceptions import (
    ConflictError,
    EntitlementDeniedError,
    QuotaExceededError,
    ValidationError,
)
from app.db.session import get_db
from app.dependencies import (
    get_entitlement_service,
    get_workspace_auth_service,
)
from app.models.project import Project
from app.models.tracking import Competitor, ProjectKeyword, ProjectProvider
from app.models.user import User
from app.services.entitlement_service import EntitlementService
from app.services.project_provider_service import ProjectProviderService
from app.services.project_service import ProjectService, ProjectUpdateInput
from app.services.prompt_set_service import PromptSetService
from app.services.tracking_service import (
    CompetitorCreateInput,
    CompetitorService,
    KeywordCreateInput,
    KeywordService,
)
from app.services.workspace_auth_service import WorkspaceAuthorizationService
from app.web.dependencies import get_web_csrf_token, require_web_user

router = APIRouter(tags=["web-project-config"])
templates = Jinja2Templates(directory="app/templates")


@router.get(
    "/app/w/{workspace_id}/projects/{project_id}/settings",
    response_class=HTMLResponse,
)
def project_settings_page(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> HTMLResponse:
    """Project configuration page - brand, topics, competitors, providers."""
    auth_service.require_membership(workspace_id, user.id)
    project = db.get(Project, project_id)
    if project is None or project.workspace_id != workspace_id:
        return templates.TemplateResponse(request, "errors/404.html", status_code=404)

    keywords = list(
        db.execute(select(ProjectKeyword).where(ProjectKeyword.project_id == project_id)).scalars()
    )
    competitors = list(
        db.execute(select(Competitor).where(Competitor.project_id == project_id)).scalars()
    )
    providers = list(
        db.execute(
            select(ProjectProvider).where(ProjectProvider.project_id == project_id)
        ).scalars()
    )

    ent = entitlement_service.get_effective_entitlements(workspace_id)
    allowed_providers = [p for p in LLMProvider if p in ent.allowed_providers]

    from app.web.pages import _build_context

    ctx = _build_context(
        request,
        user,
        workspace_id,
        csrf_token,
        db,
        auth_service,
        entitlement_service,
        project=project,
        keywords=keywords,
        competitors=competitors,
        providers=providers,
        allowed_providers=allowed_providers,
        is_owner_or_admin=auth_service.get_role(workspace_id, user.id)
        in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN),
    )
    return templates.TemplateResponse(request, "projects/settings.html", ctx.to_dict())


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/settings/brand",
)
def update_brand(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    name: Annotated[str, Form()] = "",
    domain: Annotated[str, Form()] = "",
    brand_name: Annotated[str, Form()] = "",
    brand_aliases: Annotated[str, Form()] = "",
    industry: Annotated[str, Form()] = "",
    target_country: Annotated[str, Form()] = "",
    target_language: Annotated[str, Form()] = "",
    target_audience: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Update brand details. Requires ADMIN. 0 AI Checks."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    svc = ProjectService(session=db)
    aliases = [a.strip() for a in brand_aliases.split(",") if a.strip()] if brand_aliases else None
    svc.update_project(
        workspace_id,
        project_id,
        ProjectUpdateInput(
            name=name or None,
            domain=domain or None,
            brand_name=brand_name or None,
            brand_aliases=aliases,
            industry=industry or None,
            target_country=target_country or None,
            target_language=target_language or None,
            target_audience=target_audience or None,
        ),
    )
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}/settings?saved=brand",
        status_code=302,
    )


# --- Topic (keyword) management ---


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/settings/topics/add",
)
def add_topic(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    text: Annotated[str, Form()] = "",
    intent: Annotated[str, Form()] = "",
    funnel_stage: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Add a tracking topic. Requires ADMIN. 0 AI Checks."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    svc = KeywordService(db, entitlement_service=entitlement_service)
    funnel: FunnelStage | None = None
    if funnel_stage:
        try:
            funnel = FunnelStage(funnel_stage)
        except ValueError:
            funnel = None
    try:
        svc.add_keyword(
            workspace_id,
            project_id,
            KeywordCreateInput(text=text, intent=intent or None, funnel_stage=funnel),
        )
    except (ConflictError, QuotaExceededError, ValidationError):
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}/settings?error=topic_exists",
            status_code=302,
        )
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}/settings?saved=topic",
        status_code=302,
    )


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/settings/topics/{keyword_id}/remove",
)
def remove_topic(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    keyword_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> RedirectResponse:
    """Remove a tracking topic. Requires ADMIN. 0 AI Checks."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    from app.repositories.keyword_repository import ProjectKeywordRepository

    project = db.get(Project, project_id)
    if project is None or project.workspace_id != workspace_id:
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}/settings",
            status_code=302,
        )
    repo = ProjectKeywordRepository(db)
    kw = repo.get_in_project_for_update(keyword_id, project.id)
    if kw is not None:
        kw.active = False
        project.prompt_input_revision += 1
        db.commit()
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}/settings?saved=topic_removed",
        status_code=302,
    )


# --- Competitor management ---


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/settings/competitors/add",
)
def add_competitor(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    name: Annotated[str, Form()] = "",
    domain: Annotated[str, Form()] = "",
    aliases: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Add a competitor. Requires ADMIN. 0 AI Checks."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    svc = CompetitorService(db, entitlement_service=entitlement_service)
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()] if aliases else None
    try:
        svc.add_competitor(
            workspace_id,
            project_id,
            CompetitorCreateInput(name=name, domain=domain, aliases=alias_list),
        )
    except (ConflictError, QuotaExceededError, ValidationError):
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}/settings?error=competitor_exists",
            status_code=302,
        )
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}/settings?saved=competitor",
        status_code=302,
    )


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/settings/competitors/{competitor_id}/remove",
)
def remove_competitor(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    competitor_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> RedirectResponse:
    """Remove a competitor. Requires ADMIN. 0 AI Checks."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    from app.repositories.competitor_repository import CompetitorRepository

    project = db.get(Project, project_id)
    if project is None or project.workspace_id != workspace_id:
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}/settings",
            status_code=302,
        )
    repo = CompetitorRepository(db)
    comp = repo.get_in_project_for_update(competitor_id, project.id)
    if comp is not None:
        comp.active = False
        project.prompt_input_revision += 1
        db.commit()
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}/settings?saved=competitor_removed",
        status_code=302,
    )


# --- Provider management ---


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/settings/providers",
)
def update_providers(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    providers: list[str] = Form(default_factory=list),
) -> RedirectResponse:
    """Set enabled providers. Requires ADMIN. 0 AI Checks.

    Only providers allowed by EffectiveEntitlements are accepted.
    """
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    svc = ProjectProviderService(db, entitlement_service=entitlement_service)
    parsed: list[LLMProvider] = []
    for p in providers:
        try:
            parsed.append(LLMProvider(p))
        except ValueError:
            continue
    try:
        svc.set_providers(workspace_id, project_id, parsed)
    except EntitlementDeniedError:
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}/settings?error=provider_not_allowed",
            status_code=302,
        )
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}/settings?saved=providers",
        status_code=302,
    )


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/prompts/regenerate",
)
def regenerate_prompts(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> RedirectResponse:
    """Regenerate tracking prompts. Requires OWNER or ADMIN. 0 AI Checks."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    svc = PromptSetService(session=db)
    svc.regenerate_prompt_set(
        workspace_id=workspace_id,
        project_id=project_id,
        created_by_user_id=user.id,
    )
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}?saved=prompts_regenerated",
        status_code=302,
    )
