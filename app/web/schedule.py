"""Web schedule routes — enable/disable scheduled measurements, presets.

Calls ScheduledScanService for schedule management. Backend remains
the final entitlement authority.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import WorkspaceRole
from app.db.session import get_db
from app.dependencies import (
    get_entitlement_service,
    get_workspace_auth_service,
)
from app.models.project_scan_schedule import ProjectScanSchedule
from app.models.user import User
from app.services.entitlement_service import EntitlementService
from app.services.workspace_auth_service import WorkspaceAuthorizationService
from app.web.dependencies import get_web_csrf_token, require_web_user

router = APIRouter(tags=["web-schedule"])


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
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    interval_hours: Annotated[int, Form()] = 24,
) -> RedirectResponse:
    """Enable or update scheduled measurements. Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    ent = entitlement_service.get_effective_entitlements(workspace_id)
    min_interval = ent.min_scheduled_scan_interval_hours
    if min_interval is None:
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}?error=schedule_unavailable",
            status_code=302,
        )
    if interval_hours < min_interval:
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}?error=interval_too_short",
            status_code=302,
        )

    # Upsert schedule
    schedule = db.execute(
        select(ProjectScanSchedule).where(
            ProjectScanSchedule.project_id == project_id,
            ProjectScanSchedule.workspace_id == workspace_id,
        )
    ).scalar_one_or_none()

    if schedule is None:
        schedule = ProjectScanSchedule(
            workspace_id=workspace_id,
            project_id=project_id,
            enabled=True,
            interval_hours=interval_hours,
        )
        db.add(schedule)
    else:
        schedule.enabled = True
        schedule.interval_hours = interval_hours
    db.commit()
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
    schedule = db.execute(
        select(ProjectScanSchedule).where(
            ProjectScanSchedule.project_id == project_id,
            ProjectScanSchedule.workspace_id == workspace_id,
        )
    ).scalar_one_or_none()
    if schedule is not None:
        schedule.enabled = False
        db.commit()
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}?saved=schedule_disabled",
        status_code=302,
    )
