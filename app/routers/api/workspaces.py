"""Workspace API router.

Endpoints:
  GET   /api/v1/workspaces           — list user's workspaces
  POST  /api/v1/workspaces           — create a workspace
  GET   /api/v1/workspaces/{id}      — get a workspace (member-only)
  PATCH /api/v1/workspaces/{id}      — update a workspace (OWNER/ADMIN)
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.dependencies import get_workspace_service, require_authenticated_user
from app.models.user import User
from app.schemas.auth import (
    WorkspaceCreateRequest,
    WorkspaceResponse,
    WorkspaceUpdateRequest,
)
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceResponse])
def list_workspaces(
    user: Annotated[User, Depends(require_authenticated_user)],
    ws_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> list[WorkspaceResponse]:
    """Return all workspaces where the current user is a member."""
    workspaces = ws_service.list_workspaces(user.id)
    return [WorkspaceResponse.model_validate(ws) for ws in workspaces]


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
def create_workspace(
    request: WorkspaceCreateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    ws_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> WorkspaceResponse:
    """Create a new workspace. The creator becomes OWNER."""
    ws = ws_service.create_workspace(user.id, request.name, request.workspace_type)
    return WorkspaceResponse.model_validate(ws)


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
def get_workspace(
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    ws_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> WorkspaceResponse:
    """Return a workspace if the user is a member. Non-members get 404."""
    ws = ws_service.get_workspace(workspace_id, user.id)
    return WorkspaceResponse.model_validate(ws)


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
def update_workspace(
    workspace_id: uuid.UUID,
    request: WorkspaceUpdateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    ws_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> WorkspaceResponse:
    """Update a workspace name. Requires OWNER or ADMIN role."""
    ws = ws_service.update_workspace(workspace_id, user.id, request.name)
    return WorkspaceResponse.model_validate(ws)
