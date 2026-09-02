"""Plan & Usage customer-facing page (read-only).

Displays:
- Current plan name and friendly billing source label
- AI Checks: used, reserved, remaining, monthly limit
- Resource limits: projects, topics, competitors, team members
- Feature flags: confidence, verification, scheduled scans, email

Uses EntitlementService, QuotaService, and approved read services.
Never exposes internal IDs (BillingAccount UUID, external_customer_id, etc.).
Zero AI Checks consumed by GET.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import fastapi
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.enums import BillingAccountStatus, BillingSource
from app.db.session import get_db
from app.dependencies import (
    get_entitlement_service as get_entitlement_service_dep,
)
from app.dependencies import (
    get_workspace_auth_service,
)
from app.models.user import User
from app.services.entitlement_service import EntitlementService
from app.services.quota_service import QuotaService
from app.services.workspace_auth_service import WorkspaceAuthorizationService
from app.web.dependencies import get_web_csrf_token, require_web_user
from app.web.pages import _build_context

router = APIRouter(tags=["web-plan"])
templates = Jinja2Templates(directory="app/templates")

# Friendly labels for billing sources (never expose enum names directly).
_BILLING_SOURCE_LABELS: dict[str, str] = {
    BillingSource.APPSUMO.value: "AppSumo Lifetime Deal",
    BillingSource.STRIPE.value: "Direct subscription",
    BillingSource.ADMIN.value: "Beta / complimentary access",
}

# Friendly labels for billing account statuses.
_STATUS_LABELS: dict[str, str] = {
    BillingAccountStatus.ACTIVE.value: "Active",
    BillingAccountStatus.PAST_DUE.value: "Past due",
    BillingAccountStatus.CANCELED.value: "Canceled",
    BillingAccountStatus.TRIALING.value: "Trialing",
}


@router.get(
    "/app/w/{workspace_id}/settings/plan",
    response_class=HTMLResponse,
)
def plan_page(
    request: fastapi.Request,
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service_dep)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> HTMLResponse:
    """Render the read-only plan & usage page for the workspace."""
    auth_service.require_membership(workspace_id, user.id)

    ent = entitlement_service.get_effective_entitlements(workspace_id)
    quota_svc = QuotaService(db)
    snapshot = quota_svc.get_usage_snapshot(workspace_id)

    # Calculate usage percentage and warning states.
    limit = snapshot.limit
    used = snapshot.used
    reserved = snapshot.reserved
    remaining = max(0, limit - used - reserved)
    usage_pct = int(used / limit * 100) if limit > 0 else 0
    reserved_pct = int(reserved / limit * 100) if limit > 0 else 0
    is_80_warning = (used + reserved) >= int(limit * 0.8) if limit > 0 else False
    is_exhausted = remaining == 0 and limit > 0

    # Friendly labels.
    billing_source_value = (
        ent.billing_source
        if isinstance(ent.billing_source, str)
        else (ent.billing_source.value if ent.billing_source else "")
    )
    billing_source_label = _BILLING_SOURCE_LABELS.get(billing_source_value, "Not configured")
    status_label = "Unentitled" if ent.is_unentitled else "Active"

    # Feature availability.
    features = {
        "confidence_scans": ent.confidence_scans_enabled,
        "verification_scans": ent.verification_scans_enabled,
        "scheduled_scans": ent.min_scheduled_scan_interval_hours is not None,
        "white_label_reports": ent.white_label_reports,
        "exports": ent.exports_enabled,
        "agency_dashboard": ent.agency_dashboard,
        "integrations": ent.integrations_enabled,
        "byok": ent.byok_enabled,
    }

    # Allowed providers as friendly labels.
    provider_labels = [p.value.title() for p in sorted(ent.allowed_providers)]

    # Build the shared app context (workspace, role, quota, notifications, etc.)
    ctx = _build_context(
        request,
        user,
        workspace_id,
        csrf_token,
        db,
        auth_service,
        entitlement_service,
        is_unentitled=ent.is_unentitled,
        billing_source_label=billing_source_label,
        status_label=status_label,
        plan_quota={
            "used": used,
            "reserved": reserved,
            "remaining": remaining,
            "limit": limit,
            "usage_pct": usage_pct,
            "reserved_pct": reserved_pct,
            "is_80_warning": is_80_warning,
            "is_exhausted": is_exhausted,
        },
        limits={
            "max_projects": ent.max_projects,
            "max_keywords_per_project": ent.max_keywords_per_project,
            "max_competitors_per_project": ent.max_competitors_per_project,
            "max_team_members": ent.max_team_members,
        },
        features=features,
        providers=provider_labels,
        min_scheduled_scan_interval_hours=ent.min_scheduled_scan_interval_hours,
    )

    return templates.TemplateResponse(
        request,
        "settings/plan.html",
        {
            "request": request,
            "user": ctx.user,
            "workspace": ctx.workspace,
            "workspace_id": ctx.workspace_id,
            "role": ctx.role,
            "csrf_token": ctx.csrf_token,
            "unread_notifications": ctx.unread_notifications,
            "quota": ctx.quota,
            "is_owner_or_admin": ctx.is_owner_or_admin,
            "plan_name": ctx.plan_name,
            "user_workspaces": ctx.extra["user_workspaces"],
            "is_unentitled": ent.is_unentitled,
            "billing_source_label": billing_source_label,
            "status_label": status_label,
            "plan_quota": {
                "used": used,
                "reserved": reserved,
                "remaining": remaining,
                "limit": limit,
                "usage_pct": usage_pct,
                "reserved_pct": reserved_pct,
                "is_80_warning": is_80_warning,
                "is_exhausted": is_exhausted,
            },
            "limits": {
                "max_projects": ent.max_projects,
                "max_keywords_per_project": ent.max_keywords_per_project,
                "max_competitors_per_project": ent.max_competitors_per_project,
                "max_team_members": ent.max_team_members,
            },
            "features": features,
            "providers": provider_labels,
            "min_scheduled_scan_interval_hours": ent.min_scheduled_scan_interval_hours,
        },
    )
