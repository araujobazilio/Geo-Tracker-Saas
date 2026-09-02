"""Web page routes — root, app redirect, workspace dashboard, project pages.

These routes are thin: they call the existing service layer and render
Jinja2 templates. No business logic lives here.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.enums import WorkspaceRole
from app.db.session import get_db
from app.dependencies import (
    get_entitlement_service,
    get_workspace_auth_service,
    get_workspace_service,
)
from app.models.user import User
from app.services.entitlement_service import EntitlementService
from app.services.workspace_auth_service import WorkspaceAuthorizationService
from app.services.workspace_service import WorkspaceService
from app.web.context import WebContext, resolve_role
from app.web.dashboard_service import DashboardQueryService
from app.web.dependencies import get_web_csrf_token, get_web_user, require_web_user
from app.web.view_models import format_metric_or_none, format_percent, truncate

router = APIRouter(tags=["web-pages"])
templates = Jinja2Templates(directory="app/templates")


def _build_context(
    request: Request,
    user: User,
    workspace_id: uuid.UUID,
    csrf_token: str,
    db: Session,
    auth_service: WorkspaceAuthorizationService,
    entitlement_service: EntitlementService,
    **extra: object,
) -> WebContext:
    """Build the shared WebContext for an authenticated page."""
    from sqlalchemy import func, select

    from app.models.notification import Notification

    role = resolve_role(auth_service, workspace_id, user.id)
    ws = auth_service._member_repo.get_membership(workspace_id, user.id)
    workspace = ws.workspace if ws is not None else None

    # Unread notifications
    unread = (
        db.execute(
            select(func.count(Notification.id)).where(
                Notification.workspace_id == workspace_id,
                Notification.user_id == user.id,
                Notification.read_at.is_(None),
            )
        ).scalar()
        or 0
    )

    # Quota
    dashboard_svc = DashboardQueryService(db, entitlement_service=entitlement_service)
    quota = dashboard_svc._get_quota_summary(workspace_id)

    # Plan name
    ent = entitlement_service.get_effective_entitlements(workspace_id)
    plan_name = ent.plan_code if ent.plan_code != "UNENTITLED" else ""

    ctx = WebContext(
        user=user,
        workspace=workspace,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        role=role,
        csrf_token=csrf_token,
        unread_notifications=unread,
        quota=quota,
        is_owner_or_admin=role in (WorkspaceRole.OWNER, WorkspaceRole.ADMIN),
        plan_name=plan_name,
    )
    ctx.extra.update(extra)
    return ctx


# --- Root routing ---


@router.get("/")
def root(
    user: Annotated[User | None, Depends(get_web_user)],
) -> RedirectResponse:
    """Root: redirect to /login (anonymous) or /app (authenticated)."""
    if user is not None:
        return RedirectResponse(url="/app", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/app")
def app_root(
    user: Annotated[User, Depends(require_web_user)],
    ws_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> RedirectResponse:
    """Redirect to the first accessible workspace, or show empty state."""
    workspaces = ws_service.list_workspaces(user.id)
    if not workspaces:
        # No workspaces — show a safe empty state page
        return RedirectResponse(url="/app/no-workspace", status_code=302)
    first = workspaces[0]
    return RedirectResponse(url=f"/app/w/{first.id}", status_code=302)


@router.get("/app/no-workspace", response_class=HTMLResponse)
def no_workspace(
    request: Request,
    user: Annotated[User, Depends(require_web_user)],
) -> HTMLResponse:
    """Empty state for users with no workspaces."""
    return templates.TemplateResponse(
        request,
        "dashboard/no_workspace.html",
        {"user": user},
    )


# --- Workspace dashboard ---


@router.get("/app/w/{workspace_id}", response_class=HTMLResponse)
def workspace_dashboard(
    request: Request,
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> HTMLResponse:
    """Workspace overview dashboard."""
    auth_service.require_membership(workspace_id, user.id)
    dashboard_svc = DashboardQueryService(db, entitlement_service=entitlement_service)
    overview = dashboard_svc.get_workspace_overview(workspace_id, user.id)
    ctx = _build_context(
        request,
        user,
        workspace_id,
        csrf_token,
        db,
        auth_service,
        entitlement_service,
        overview=overview,
        format_percent=format_percent,
        format_metric_or_none=format_metric_or_none,
        truncate=truncate,
    )
    return templates.TemplateResponse(request, "dashboard/workspace.html", ctx.to_dict())


# --- Project dashboard ---


@router.get(
    "/app/w/{workspace_id}/projects/{project_id}",
    response_class=HTMLResponse,
)
def project_dashboard(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> HTMLResponse:
    """Project dashboard — the primary daily product screen."""
    auth_service.require_membership(workspace_id, user.id)
    dashboard_svc = DashboardQueryService(db, entitlement_service=entitlement_service)
    data = dashboard_svc.get_project_dashboard(workspace_id, project_id)
    ctx = _build_context(
        request,
        user,
        workspace_id,
        csrf_token,
        db,
        auth_service,
        entitlement_service,
        project_data=data,
        format_percent=format_percent,
        format_metric_or_none=format_metric_or_none,
        truncate=truncate,
    )
    return templates.TemplateResponse(request, "projects/dashboard.html", ctx.to_dict())
