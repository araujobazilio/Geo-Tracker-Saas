"""Notifications and preferences API router.

Endpoints:
  GET    /api/v1/workspaces/{ws}/notifications               — list (filtered)
  PATCH  /api/v1/workspaces/{ws}/notifications/{nid}/read     — mark read
  POST   /api/v1/workspaces/{ws}/notifications/read-all       — mark all read
  POST   /api/v1/workspaces/{ws}/notifications/{nid}/email/retry — retry email

  GET    /api/v1/workspaces/{ws}/notification-preferences     — get own
  PUT    /api/v1/workspaces/{ws}/notification-preferences     — update own
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select

from app.core.enums import EmailDeliveryStatus, WorkspaceRole
from app.core.exceptions import NotFoundError, ValidationError
from app.dependencies import (
    get_entitlement_service,
    get_workspace_auth_service,
    require_authenticated_user,
)
from app.models.email_delivery import EmailDelivery
from app.models.notification import Notification
from app.models.user import User
from app.schemas.notifications import (
    EmailRetryResponse,
    NotificationListResponse,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdateRequest,
    NotificationResponse,
)
from app.services.entitlement_service import EntitlementService
from app.services.notification_service import NotificationService
from app.services.workspace_auth_service import WorkspaceAuthorizationService

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["notifications"])


def _build_notification_service(
    entitlement_service: EntitlementService,
) -> NotificationService:
    return NotificationService(entitlement_service._session)


# --- Notifications ---


@router.get("/notifications", response_model=NotificationListResponse)
def list_notifications(
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    unread_only: bool = Query(False),
    notification_type: str | None = Query(None),
    project_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> NotificationListResponse:
    auth_service.require_membership(workspace_id, user.id)

    session = entitlement_service._session
    query = select(Notification).where(
        Notification.workspace_id == workspace_id,
        Notification.user_id == user.id,
    )
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    if notification_type:
        query = query.where(Notification.notification_type == notification_type)
    if project_id:
        query = query.where(Notification.project_id == project_id)

    count_query = select(func.count()).select_from(query.subquery())
    total = session.execute(count_query).scalar() or 0

    rows = (
        session.execute(query.order_by(Notification.created_at.desc()).limit(limit).offset(offset))
        .scalars()
        .all()
    )

    items = [NotificationResponse.model_validate(n) for n in rows]
    return NotificationListResponse(
        items=items,
        total=total,
        has_more=(offset + len(items)) < total,
    )


@router.patch("/notifications/{notification_id}/read", response_model=NotificationResponse)
def mark_notification_read(
    workspace_id: uuid.UUID,
    notification_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> NotificationResponse:
    auth_service.require_membership(workspace_id, user.id)

    service = _build_notification_service(entitlement_service)
    updated = service.mark_read(notification_id, user.id)
    if not updated:
        raise NotFoundError("Notification not found.")

    session = entitlement_service._session
    notification = session.get(Notification, notification_id)
    return NotificationResponse.model_validate(notification)


@router.post("/notifications/read-all", status_code=status.HTTP_200_OK)
def mark_all_read(
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> dict[str, int]:
    auth_service.require_membership(workspace_id, user.id)

    service = _build_notification_service(entitlement_service)
    count = service.mark_all_read(user.id, workspace_id)
    return {"marked_read": count}


# --- Email Retry ---


@router.post(
    "/notifications/{notification_id}/email/retry",
    response_model=EmailRetryResponse,
)
def retry_email(
    workspace_id: uuid.UUID,
    notification_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> EmailRetryResponse:
    # Only OWNER/ADMIN can retry email.
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)

    session = entitlement_service._session

    # Load notification and verify workspace.
    notification = (
        session.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.workspace_id == workspace_id,
            )
        )
        .scalars()
        .first()
    )
    if notification is None:
        raise NotFoundError("Notification not found.")

    delivery = (
        session.execute(
            select(EmailDelivery).where(EmailDelivery.notification_id == notification_id)
        )
        .scalars()
        .first()
    )
    if delivery is None:
        raise NotFoundError("Email delivery not found.")

    if delivery.status != EmailDeliveryStatus.FAILED:
        raise ValidationError("Email delivery is not in FAILED state.")

    # Reset to PENDING and re-dispatch.
    delivery.status = EmailDeliveryStatus.PENDING
    delivery.failure_code = None
    delivery.failure_message = None
    session.commit()

    # Dispatch email task.
    try:
        from app.workers.notification_tasks import send_email_task

        send_email_task.delay(str(delivery.id))
    except Exception:
        pass  # Sweeper will pick it up.

    return EmailRetryResponse(
        email_delivery_id=delivery.id,
        status="PENDING",
        message="Email delivery reset to PENDING and re-dispatched.",
    )


# --- Notification Preferences ---


@router.get("/notification-preferences", response_model=NotificationPreferenceResponse)
def get_preferences(
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> NotificationPreferenceResponse:
    auth_service.require_membership(workspace_id, user.id)

    service = _build_notification_service(entitlement_service)
    pref = service.get_or_create_preference(workspace_id, user.id)
    session = entitlement_service._session
    session.commit()
    return NotificationPreferenceResponse.model_validate(pref)


@router.put("/notification-preferences", response_model=NotificationPreferenceResponse)
def update_preferences(
    workspace_id: uuid.UUID,
    body: NotificationPreferenceUpdateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> NotificationPreferenceResponse:
    auth_service.require_membership(workspace_id, user.id)

    service = _build_notification_service(entitlement_service)
    pref = service.get_or_create_preference(workspace_id, user.id)
    pref.email_enabled = body.email_enabled
    pref.scheduled_scan_summary = body.scheduled_scan_summary
    pref.high_priority_opportunities = body.high_priority_opportunities
    pref.verification_outcomes = body.verification_outcomes
    session = entitlement_service._session
    session.commit()
    return NotificationPreferenceResponse.model_validate(pref)
