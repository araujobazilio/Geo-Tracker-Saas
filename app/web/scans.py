"""Web scan routes - run measurement, polling, scan detail, confidence.

Calls ScanCreationService for scan creation. The polling endpoint only
queries PostgreSQL - never calls providers or re-dispatches scans.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.enums import ScanType, WorkspaceRole
from app.db.session import get_db
from app.dependencies import (
    get_audit_service,
    get_entitlement_service,
    get_workspace_auth_service,
)
from app.models.user import User
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService
from app.services.scan_creation_service import ScanCreationService
from app.services.scanning.dispatcher import ScanDispatcher
from app.services.workspace_auth_service import WorkspaceAuthorizationService
from app.web.dashboard_service import DashboardQueryService
from app.web.dependencies import get_web_csrf_token, get_web_scan_dispatcher, require_web_user
from app.web.view_models import scan_status_badge, scan_status_label, scan_type_label

router = APIRouter(tags=["web-scans"])
templates = Jinja2Templates(directory="app/templates")


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/scans",
)
def run_scan(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    dispatcher: Annotated[ScanDispatcher, Depends(get_web_scan_dispatcher)],
    idempotency_key: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Run a measurement (STANDARD scan). Requires OWNER or ADMIN."""
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)

    # Check prompt set staleness - refuse if stale
    dashboard_svc = DashboardQueryService(db, entitlement_service=entitlement_service)
    if dashboard_svc._check_prompt_set_stale(workspace_id, project_id):
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}?error=stale_prompts",
            status_code=302,
        )

    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())

    svc = ScanCreationService(
        session=db,
        dispatcher=dispatcher,
        audit_service=audit,
    )
    result = svc.create_scan(
        workspace_id=workspace_id,
        project_id=project_id,
        scan_type=ScanType.STANDARD,
        requested_by_user_id=user.id,
        idempotency_key=idempotency_key,
    )
    scan = result.scan
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}/scans/{scan.id}",
        status_code=302,
    )


@router.get(
    "/app/w/{workspace_id}/projects/{project_id}/scans/{scan_id}",
    response_class=HTMLResponse,
)
def scan_detail(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> HTMLResponse:
    """Scan detail page with status, metrics, and evidence."""
    auth_service.require_membership(workspace_id, user.id)
    dashboard_svc = DashboardQueryService(db, entitlement_service=entitlement_service)
    scan = dashboard_svc.get_scan_detail(workspace_id, scan_id)
    if scan is None or scan.project_id != project_id:
        return templates.TemplateResponse(request, "errors/404.html", status_code=404)

    evidence = dashboard_svc.get_scan_evidence(workspace_id, project_id, scan_id)
    metrics_summary = dashboard_svc.get_scan_metrics_summary(workspace_id, project_id, scan_id)

    # Confidence result UX: if this scan is a CONFIDENCE scan with a
    # completed analysis, load the reliability metrics for display.
    confidence_result: dict[str, object] | None = None
    if scan.scan_type == ScanType.CONFIDENCE:
        confidence_result = dashboard_svc.get_confidence_result(workspace_id, project_id, scan_id)

    # Server-rendered stable idempotency key for the confidence form.
    confidence_idempotency_key = str(uuid.uuid4())

    # Entitlement flag for the confidence button.
    ent = entitlement_service.get_effective_entitlements(workspace_id)

    from app.web.pages import _build_context

    ctx = _build_context(
        request,
        user,
        workspace_id,
        csrf_token,
        db,
        auth_service,
        entitlement_service,
        scan=scan,
        scan_status_label=scan_status_label,
        scan_status_badge=scan_status_badge,
        scan_type_label=scan_type_label,
        project_id=str(project_id),
        evidence=evidence,
        metrics_summary=metrics_summary,
        confidence_result=confidence_result,
        confidence_idempotency_key=confidence_idempotency_key,
        confidence_scans_enabled=ent.confidence_scans_enabled,
    )
    return templates.TemplateResponse(request, "scans/detail.html", ctx.to_dict())


@router.get(
    "/app/w/{workspace_id}/projects/{project_id}/scans/{scan_id}/status",
    response_class=HTMLResponse,
)
def scan_status_poll(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
) -> HTMLResponse:
    """Lightweight polling endpoint for scan status.

    Only queries PostgreSQL. Never calls providers or re-dispatches.
    Returns a partial template with the current status.
    """
    auth_service.require_membership(workspace_id, user.id)
    dashboard_svc = DashboardQueryService(db, entitlement_service=entitlement_service)
    scan = dashboard_svc.get_scan_status(workspace_id, scan_id)
    if scan is None or scan.project_id != project_id:
        return HTMLResponse("<!-- not found -->", status_code=404)

    is_terminal = scan.status in ("COMPLETED", "PARTIAL", "FAILED", "CANCELED")
    return templates.TemplateResponse(
        request,
        "partials/scan_status.html",
        {
            "scan": scan,
            "is_terminal": is_terminal,
            "workspace_id": str(workspace_id),
            "project_id": str(project_id),
            "scan_status_label": scan_status_label,
            "scan_status_badge": scan_status_badge,
        },
    )


@router.post(
    "/app/w/{workspace_id}/projects/{project_id}/scans/{scan_id}/confidence",
)
def run_confidence_scan(
    request: Request,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    db: Annotated[Session, Depends(get_db)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    csrf_token: Annotated[str, Depends(get_web_csrf_token)],
    dispatcher: Annotated[ScanDispatcher, Depends(get_web_scan_dispatcher)],
    idempotency_key: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Run a confidence (reliability) check on a completed measurement.

    Requires OWNER or ADMIN. Uses ConfidenceScanCreationService.
    """
    auth_service.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)

    ent = entitlement_service.get_effective_entitlements(workspace_id)
    if not ent.confidence_scans_enabled:
        return RedirectResponse(
            url=f"/app/w/{workspace_id}/projects/{project_id}/scans/{scan_id}?error=confidence_unavailable",
            status_code=302,
        )

    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())

    from app.services.confidence_scan_creation_service import ConfidenceScanCreationService

    svc = ConfidenceScanCreationService(
        session=db,
        dispatcher=dispatcher,
        audit_service=audit,
    )
    result = svc.create_confidence_scan(
        workspace_id=workspace_id,
        project_id=project_id,
        baseline_scan_id=scan_id,
        requested_by_user_id=user.id,
        idempotency_key=idempotency_key,
    )
    return RedirectResponse(
        url=f"/app/w/{workspace_id}/projects/{project_id}/scans/{result.scan.id}",
        status_code=302,
    )
