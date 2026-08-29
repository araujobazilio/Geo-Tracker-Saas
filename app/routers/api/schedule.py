"""Schedule API router.

Endpoints:
  GET    /api/v1/workspaces/{ws}/projects/{pid}/schedule  — get schedule
  PUT    /api/v1/workspaces/{ws}/projects/{pid}/schedule  — create/replace
  DELETE /api/v1/workspaces/{ws}/projects/{pid}/schedule  — disable
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.enums import WorkspaceRole
from app.core.exceptions import NotFoundError
from app.dependencies import (
    get_entitlement_service,
    get_workspace_auth_service,
    require_authenticated_user,
)
from app.models.user import User
from app.schemas.notifications import ScheduleCreateRequest, ScheduleResponse
from app.services.entitlement_service import EntitlementService
from app.services.scheduled_scan_service import ScheduledScanService
from app.services.workspace_auth_service import WorkspaceAuthorizationService

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/projects/{project_id}/schedule", tags=["schedule"]
)


def _build_service(
    entitlement_service: EntitlementService,
) -> ScheduledScanService:
    from app.services.audit_service import AuditService
    from app.services.scanning.dispatcher import CeleryScanDispatcher

    session = entitlement_service._session
    audit = AuditService()
    return ScheduledScanService(
        session,
        dispatcher=CeleryScanDispatcher(),
        audit_service=audit,
    )


@router.get("", response_model=ScheduleResponse)
def get_schedule(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> ScheduleResponse:
    auth_service.require_membership(workspace_id, user.id)

    service = _build_service(entitlement_service)
    schedule = service.get_schedule(workspace_id, project_id)
    if schedule is None:
        raise NotFoundError("Schedule not found.")

    ent = entitlement_service.get_effective_entitlements(workspace_id)
    return ScheduleResponse(
        id=schedule.id,
        workspace_id=schedule.workspace_id,
        project_id=schedule.project_id,
        enabled=schedule.enabled,
        interval_hours=schedule.interval_hours,
        minimum_allowed_interval_hours=ent.min_scheduled_scan_interval_hours,
        next_run_at=schedule.next_run_at,
        last_due_at=schedule.last_due_at,
        last_triggered_at=schedule.last_triggered_at,
        last_scan_id=schedule.last_scan_id,
        last_outcome=schedule.last_outcome,
        last_skip_reason=schedule.last_skip_reason,
        created_by_user_id=schedule.created_by_user_id,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


@router.put("", response_model=ScheduleResponse, status_code=status.HTTP_200_OK)
def put_schedule(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    body: ScheduleCreateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> ScheduleResponse:
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)

    service = _build_service(entitlement_service)
    schedule = service.create_or_update_schedule(
        workspace_id=workspace_id,
        project_id=project_id,
        enabled=body.enabled,
        interval_hours=body.interval_hours,
        created_by_user_id=user.id,
        first_run_at=body.first_run_at,
    )

    ent = entitlement_service.get_effective_entitlements(workspace_id)
    return ScheduleResponse(
        id=schedule.id,
        workspace_id=schedule.workspace_id,
        project_id=schedule.project_id,
        enabled=schedule.enabled,
        interval_hours=schedule.interval_hours,
        minimum_allowed_interval_hours=ent.min_scheduled_scan_interval_hours,
        next_run_at=schedule.next_run_at,
        last_due_at=schedule.last_due_at,
        last_triggered_at=schedule.last_triggered_at,
        last_scan_id=schedule.last_scan_id,
        last_outcome=schedule.last_outcome,
        last_skip_reason=schedule.last_skip_reason,
        created_by_user_id=schedule.created_by_user_id,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


@router.delete("", response_model=ScheduleResponse | None, status_code=status.HTTP_200_OK)
def delete_schedule(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> ScheduleResponse | None:
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)

    service = _build_service(entitlement_service)
    schedule = service.disable_schedule(workspace_id, project_id)
    if schedule is None:
        raise NotFoundError("Schedule not found.")

    ent = entitlement_service.get_effective_entitlements(workspace_id)
    return ScheduleResponse(
        id=schedule.id,
        workspace_id=schedule.workspace_id,
        project_id=schedule.project_id,
        enabled=schedule.enabled,
        interval_hours=schedule.interval_hours,
        minimum_allowed_interval_hours=ent.min_scheduled_scan_interval_hours,
        next_run_at=schedule.next_run_at,
        last_due_at=schedule.last_due_at,
        last_triggered_at=schedule.last_triggered_at,
        last_scan_id=schedule.last_scan_id,
        last_outcome=schedule.last_outcome,
        last_skip_reason=schedule.last_skip_reason,
        created_by_user_id=schedule.created_by_user_id,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )
