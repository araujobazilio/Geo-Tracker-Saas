"""Web project configuration routes — prompt regeneration.

Calls PromptSetService. This is deterministic/local — 0 AI Checks.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.enums import WorkspaceRole
from app.db.session import get_db
from app.dependencies import (
    get_entitlement_service,
    get_workspace_auth_service,
)
from app.models.user import User
from app.services.entitlement_service import EntitlementService
from app.services.prompt_set_service import PromptSetService
from app.services.workspace_auth_service import WorkspaceAuthorizationService
from app.web.dependencies import get_web_csrf_token, require_web_user

router = APIRouter(tags=["web-project-config"])


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
