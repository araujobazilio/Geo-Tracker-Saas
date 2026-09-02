"""Web notification routes — notification center, mark read, preferences.

Reuses NotificationService. Preserves tenant isolation.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.dependencies import get_workspace_auth_service
from app.models.notification import Notification
from app.models.user import User
from app.services.notification_service import NotificationService
from app.services.workspace_auth_service import WorkspaceAuthorizationService
from app.web.dependencies import get_web_csrf_token, require_web_user

router = APIRouter(tags=["web-notifications"])
templates = Jinja2Templates(directory="app/templates")


@router.get(
    "/app/w/{workspace_id}/notifications",
    response_class=HTMLResponse,
)
def notification_center(
    request: Request,
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    filter: str | None = None,
) -> HTMLResponse:
    """Notification center page."""
    auth_service.require_membership(workspace_id, user.id)

    query = select(Notification).where(
        Notification.workspace_id == workspace_id,
        Notification.user_id == user.id,
    )
    if filter == "unread":
        query = query.where(Notification.read_at.is_(None))
    query = query.order_by(Notification.created_at.desc()).limit(50)
    notifications = list(db.execute(query).scalars())

    from app.dependencies import get_entitlement_service
    from app.web.pages import _build_context

    ent_svc = get_entitlement_service(db)
    ctx = _build_context(
        request,
        user,
        workspace_id,
        csrf_token,
        db,
        auth_service,
        ent_svc,
        notifications=notifications,
        filter=filter or "",
    )
    return templates.TemplateResponse(request, "notifications/list.html", ctx.to_dict())


@router.post(
    "/app/w/{workspace_id}/notifications/{notification_id}/read",
)
def mark_notification_read(
    request: Request,
    workspace_id: uuid.UUID,
    notification_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> RedirectResponse:
    """Mark a single notification as read."""
    auth_service.require_membership(workspace_id, user.id)
    svc = NotificationService(session=db)
    svc.mark_read(workspace_id, notification_id, user.id)
    return RedirectResponse(url=f"/app/w/{workspace_id}/notifications", status_code=302)


@router.post(
    "/app/w/{workspace_id}/notifications/mark-all-read",
)
def mark_all_notifications_read(
    request: Request,
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> RedirectResponse:
    """Mark all notifications as read for this user in this workspace."""
    auth_service.require_membership(workspace_id, user.id)
    svc = NotificationService(session=db)
    svc.mark_all_read(user.id, workspace_id)
    return RedirectResponse(url=f"/app/w/{workspace_id}/notifications", status_code=302)


@router.get(
    "/app/w/{workspace_id}/settings/notifications",
    response_class=HTMLResponse,
)
def notification_preferences_page(
    request: Request,
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> HTMLResponse:
    """Notification preferences settings page."""
    auth_service.require_membership(workspace_id, user.id)
    svc = NotificationService(session=db)
    pref = svc.get_or_create_preference(workspace_id, user.id)

    from app.dependencies import get_entitlement_service
    from app.web.pages import _build_context

    ent_svc = get_entitlement_service(db)
    ctx = _build_context(
        request,
        user,
        workspace_id,
        csrf_token,
        db,
        auth_service,
        ent_svc,
        preference=pref,
    )
    return templates.TemplateResponse(request, "settings/notifications.html", ctx.to_dict())


@router.post(
    "/app/w/{workspace_id}/settings/notifications",
)
def update_notification_preferences(
    request: Request,
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    email_enabled: Annotated[bool | None, Form()] = None,
    scheduled_scan_summary: Annotated[bool | None, Form()] = None,
    high_priority_opportunities: Annotated[bool | None, Form()] = None,
    verification_outcomes: Annotated[bool | None, Form()] = None,
) -> RedirectResponse:
    """Update notification preferences for the current user."""
    auth_service.require_membership(workspace_id, user.id)
    svc = NotificationService(session=db)
    pref = svc.get_or_create_preference(workspace_id, user.id)
    pref.email_enabled = email_enabled or False
    pref.scheduled_scan_summary = scheduled_scan_summary or False
    pref.high_priority_opportunities = high_priority_opportunities or False
    pref.verification_outcomes = verification_outcomes or False
    db.commit()
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/settings/notifications?saved=1",
        status_code=302,
    )
