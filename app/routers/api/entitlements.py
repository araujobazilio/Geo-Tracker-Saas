"""Entitlements and usage API router.

Endpoints:
  GET /api/v1/workspaces/{workspace_id}/entitlements — product capabilities
  GET /api/v1/workspaces/{workspace_id}/usage        — monthly AI Check quota

Both endpoints:
  - Require authentication
  - Require workspace membership (via WorkspaceAuthorizationService)
  - Cross-tenant access returns 404
  - No CSRF required (GET only)
  - Do not expose billing internals (customer IDs, license IDs)
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies import (
    get_entitlement_service,
    get_quota_service,
    get_workspace_auth_service,
    require_authenticated_user,
)
from app.models.user import User
from app.schemas.entitlements import EntitlementResponse, UsageResponse
from app.services.entitlement_service import EntitlementService
from app.services.quota_service import QuotaService
from app.services.workspace_auth_service import WorkspaceAuthorizationService

router = APIRouter(prefix="/api/v1/workspaces", tags=["entitlements"])


@router.get("/{workspace_id}/entitlements", response_model=EntitlementResponse)
def get_entitlements(
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    authz: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    ent_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> EntitlementResponse:
    """Return the workspace's effective entitlements (product capabilities)."""
    authz.require_membership(workspace_id, user.id)
    ent = ent_service.get_effective_entitlements(workspace_id)
    return EntitlementResponse(
        plan_code=ent.plan_code,
        billing_source=ent.billing_source,
        max_projects=ent.max_projects,
        max_keywords_per_project=ent.max_keywords_per_project,
        max_competitors_per_project=ent.max_competitors_per_project,
        max_team_members=ent.max_team_members,
        monthly_ai_checks=ent.monthly_ai_checks,
        allowed_providers=sorted(ent.allowed_providers, key=lambda p: p.value),
        min_scheduled_scan_interval_hours=ent.min_scheduled_scan_interval_hours,
        confidence_scans_enabled=ent.confidence_scans_enabled,
        verification_scans_enabled=ent.verification_scans_enabled,
        white_label_reports=ent.white_label_reports,
        exports_enabled=ent.exports_enabled,
        agency_dashboard=ent.agency_dashboard,
        integrations_enabled=ent.integrations_enabled,
        byok_enabled=ent.byok_enabled,
    )


@router.get("/{workspace_id}/usage", response_model=UsageResponse)
def get_usage(
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    authz: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    quota_service: Annotated[QuotaService, Depends(get_quota_service)],
) -> UsageResponse:
    """Return the workspace's current monthly AI Check usage."""
    authz.require_membership(workspace_id, user.id)
    snapshot = quota_service.get_usage_snapshot(workspace_id)
    return UsageResponse(
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        limit=snapshot.limit,
        used=snapshot.used,
        reserved=snapshot.reserved,
        remaining=snapshot.remaining,
        usage_percentage=snapshot.usage_percentage,
    )
