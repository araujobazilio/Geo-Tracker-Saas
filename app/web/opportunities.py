"""Web Action Center routes — opportunity list, detail, workflow, verification.

Calls OpportunityWorkflowService and VerificationScanCreationService.
The web layer never directly sets VERIFIED status.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import OpportunityStatus, WorkspaceRole
from app.db.session import get_db
from app.dependencies import (
    get_audit_service,
    get_entitlement_service,
    get_workspace_auth_service,
)
from app.models.opportunity import Opportunity
from app.models.user import User
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService
from app.services.opportunity_workflow_service import OpportunityWorkflowService
from app.services.verification_scan_creation_service import VerificationScanCreationService
from app.services.workspace_auth_service import WorkspaceAuthorizationService
from app.web.dependencies import get_web_csrf_token, require_web_user
from app.web.view_models import (
    opportunity_priority_badge,
    opportunity_priority_label,
    opportunity_status_badge,
    opportunity_status_label,
    opportunity_type_label,
)

router = APIRouter(tags=["web-opportunities"])
templates = Jinja2Templates(directory="app/templates")


@router.get(
    "/app/w/{workspace_id}/projects/{project_id}/opportunities",
    response_class=HTMLResponse,
)
def action_center(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    status: str | None = None,
    priority: str | None = None,
    opp_type: str | None = None,
) -> HTMLResponse:
    """Action Center page — list opportunities with filters."""
    auth_service.require_membership(workspace_id, user.id)

    query = select(Opportunity).where(
        Opportunity.workspace_id == workspace_id,
        Opportunity.project_id == project_id,
    )

    # Default: OPEN + IN_PROGRESS + IMPLEMENTED
    if status and status != "all":
        try:
            status_enum = OpportunityStatus(status)
            query = query.where(Opportunity.status == status_enum)
        except ValueError:
            pass
    else:
        query = query.where(
            Opportunity.status.in_(
                [
                    OpportunityStatus.OPEN,
                    OpportunityStatus.IN_PROGRESS,
                    OpportunityStatus.IMPLEMENTED,
                ]
            )
        )

    if priority:
        query = query.where(Opportunity.priority == priority)

    if opp_type:
        query = query.where(Opportunity.opportunity_type == opp_type)

    query = query.order_by(Opportunity.priority.desc(), Opportunity.first_detected_at.desc())
    opportunities = list(db.execute(query).scalars())

    from app.web.pages import _build_context

    ctx = _build_context(
        request,
        user,
        workspace_id,
        csrf_token,
        db,
        auth_service,
        entitlement_service,
        opportunities=opportunities,
        project_id=str(project_id),
        filter_status=status or "",
        filter_priority=priority or "",
        filter_type=opp_type or "",
        opportunity_priority_label=opportunity_priority_label,
        opportunity_priority_badge=opportunity_priority_badge,
        opportunity_status_label=opportunity_status_label,
        opportunity_status_badge=opportunity_status_badge,
        opportunity_type_label=opportunity_type_label,
    )
    return templates.TemplateResponse(request, "opportunities/list.html", ctx.to_dict())


@router.get(
    "/app/w/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}",
    response_class=HTMLResponse,
)
def opportunity_detail(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> HTMLResponse:
    """Opportunity detail page."""
    auth_service.require_membership(workspace_id, user.id)
    opp = db.execute(
        select(Opportunity).where(
            Opportunity.id == opportunity_id,
            Opportunity.workspace_id == workspace_id,
            Opportunity.project_id == project_id,
        )
    ).scalar_one_or_none()
    if opp is None:
        return templates.TemplateResponse(request, "errors/404.html", status_code=404)

    from app.web.pages import _build_context

    ent = entitlement_service.get_effective_entitlements(workspace_id)
    can_verify = opp.status == OpportunityStatus.IMPLEMENTED and ent.verification_scans_enabled

    ctx = _build_context(
        request,
        user,
        workspace_id,
        csrf_token,
        db,
        auth_service,
        entitlement_service,
        opportunity=opp,
        project_id=str(project_id),
        can_verify=can_verify,
        opportunity_priority_label=opportunity_priority_label,
        opportunity_priority_badge=opportunity_priority_badge,
        opportunity_status_label=opportunity_status_label,
        opportunity_status_badge=opportunity_status_badge,
        opportunity_type_label=opportunity_type_label,
    )
    return templates.TemplateResponse(request, "opportunities/detail.html", ctx.to_dict())


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}/transition",
)
def opportunity_transition(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    new_status: Annotated[str, Form()],
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> RedirectResponse:
    """Transition an opportunity status. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    try:
        target = OpportunityStatus(new_status)
    except ValueError:
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}",
            status_code=302,
        )

    svc = OpportunityWorkflowService(session=db)
    svc.transition(workspace_id, project_id, opportunity_id, target)
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}",
        status_code=302,
    )


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/opportunities/{opportunity_id}/verify",
)
def start_verification(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    idempotency_key: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Start a verification scan for an IMPLEMENTED opportunity."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)

    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())

    from app.web.scans import _get_scan_dispatcher

    dispatcher = _get_scan_dispatcher(db)
    svc = VerificationScanCreationService(
        session=db,
        dispatcher=dispatcher,  # type: ignore[arg-type]
        audit_service=audit,
    )
    result = svc.create_verification_scan(
        workspace_id=workspace_id,
        project_id=project_id,
        opportunity_id=opportunity_id,
        requested_by_user_id=user.id,
        idempotency_key=idempotency_key,
    )
    scan = result.scan
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}/scans/{scan.id}",
        status_code=302,
    )
