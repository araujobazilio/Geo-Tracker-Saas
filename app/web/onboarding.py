"""Web onboarding routes — guided project creation wizard.

The wizard uses progressive sections in the browser but submits ONE
final POST to the backend. No partial project records are created
during the wizard steps.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.enums import LLMProvider, WorkspaceRole
from app.db.session import get_db
from app.dependencies import (
    get_audit_service,
    get_entitlement_service,
    get_workspace_auth_service,
)
from app.models.user import User
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService
from app.services.project_onboarding_service import (
    CompetitorInput,
    KeywordInput,
    OnboardingRequest,
    ProjectOnboardingService,
)
from app.services.workspace_auth_service import WorkspaceAuthorizationService
from app.web.dependencies import get_web_csrf_token, require_web_user
from app.web.forms import parse_onboarding_form
from app.web.pages import _build_context
from app.web.view_models import provider_label

router = APIRouter(tags=["web-onboarding"])
templates = Jinja2Templates(directory="app/templates")


@router.get(
    "/app/w/{workspace_id}/projects/new",
    response_class=HTMLResponse,
)
def onboarding_page(
    request: Request,
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> HTMLResponse:
    """Render the guided onboarding wizard."""
    auth_service.require_membership(workspace_id, user.id)
    ent = entitlement_service.get_effective_entitlements(workspace_id)

    # Allowed providers from entitlement
    allowed_providers = [
        {"value": p.value, "label": provider_label(p)}
        for p in LLMProvider
        if p in ent.allowed_providers
    ]

    ctx = _build_context(
        request,
        user,
        workspace_id,
        csrf_token,
        db,
        auth_service,
        entitlement_service,
        allowed_providers=allowed_providers,
        max_keywords=ent.max_keywords_per_project,
        max_competitors=ent.max_competitors_per_project,
        errors={},
        form_data={},
    )
    return templates.TemplateResponse(request, "projects/onboarding.html", ctx.to_dict())


@router.post(
    "/app/w/{workspace_id}/projects/new",
    response_model=None,
)
async def onboarding_submit(
    request: Request,
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> RedirectResponse | HTMLResponse:
    """Process the onboarding wizard submission.

    Calls ProjectOnboardingService once with the complete payload.
    No partial project data is created on validation error.
    """
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    form_data: dict[str, object] = dict(await request.form())

    parsed = parse_onboarding_form(form_data)
    if not parsed.is_valid:
        ent = entitlement_service.get_effective_entitlements(workspace_id)
        allowed_providers = [
            {"value": p.value, "label": provider_label(p)}
            for p in LLMProvider
            if p in ent.allowed_providers
        ]
        ctx = _build_context(
            request,
            user,
            workspace_id,
            csrf_token,
            db,
            auth_service,
            entitlement_service,
            allowed_providers=allowed_providers,
            max_keywords=ent.max_keywords_per_project,
            max_competitors=ent.max_competitors_per_project,
            errors=parsed.errors,
            form_data=form_data,
        )
        return templates.TemplateResponse(
            request,
            "projects/onboarding.html",
            ctx.to_dict(),
            status_code=422,
        )

    onboarding_request = OnboardingRequest(
        name=parsed.name,
        domain=parsed.domain,
        brand_name=parsed.brand_name,
        brand_aliases=parsed.brand_aliases,
        industry=parsed.industry or None,
        target_country=parsed.target_country or None,
        target_language=parsed.target_language or None,
        target_audience=parsed.target_audience or None,
        keywords=[
            KeywordInput(
                text=kw["text"],
                intent=kw["intent"] or None,
                funnel_stage=kw["funnel_stage"] or None,
            )
            for kw in parsed.keywords
        ],
        competitors=[
            CompetitorInput(name=c["name"], domain=c["domain"], aliases=c.get("aliases", []))
            for c in parsed.competitors
            if c.get("name") and c.get("domain")
        ],
        providers=parsed.providers,
    )

    svc = ProjectOnboardingService(session=db, entitlement_service=entitlement_service)
    project = svc.onboard_project(
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
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project.id}",
        status_code=302,
    )
