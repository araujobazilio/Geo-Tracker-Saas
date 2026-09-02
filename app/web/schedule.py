"""Web schedule routes — enable/disable scheduled measurements, presets.

Calls ScheduledScanService for schedule management. Backend remains
the final entitlement authority.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.enums import WorkspaceRole
from app.core.exceptions import EntitlementDeniedError, ValidationError
from app.db.session import get_db
from app.dependencies import get_workspace_auth_service
from app.models.user import User
from app.services.audit_service import AuditService
from app.services.scanning.dispatcher import CeleryScanDispatcher
from app.services.scheduled_scan_service import ScheduledScanService
from app.services.workspace_auth_service import WorkspaceAuthorizationService
from app.web.dependencies import get_web_csrf_token, require_web_user

router = APIRouter(tags=["web-schedule"])


def _build_service(db: Session) -> ScheduledScanService:
    """Construct a ScheduledScanService bound to the given session."""
    return ScheduledScanService(
        db,
        dispatcher=CeleryScanDispatcher(),
        audit_service=AuditService(),
    )


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/schedule/enable",
)
def enable_schedule(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    interval_hours: Annotated[int, Form()] = 24,
) -> RedirectResponse:
    """Enable or update scheduled measurements. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    service = _build_service(db)
    try:
        service.create_or_update_schedule(
            workspace_id,
            project_id,
            enabled=True,
            interval_hours=interval_hours,
            created_by_user_id=user.id,
        )
    except EntitlementDeniedError:
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}?error=schedule_unavailable",
            status_code=302,
        )
    except ValidationError:
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}?error=interval_too_short",
            status_code=302,
        )
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}?saved=schedule",
        status_code=302,
    )


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/schedule/disable",
)
def disable_schedule(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> RedirectResponse:
    """Disable scheduled measurements. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    service = _build_service(db)
    service.disable_schedule(workspace_id, project_id)
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}?saved=schedule_disabled",
        status_code=302,
    )
