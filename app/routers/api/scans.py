"""STANDARD Scan creation and tenant-scoped evidence reads."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status

from app.core.enums import WorkspaceRole
from app.dependencies import (
    get_audit_service,
    get_entitlement_service,
    get_workspace_auth_service,
    require_authenticated_user,
)
from app.models.scan import PromptRun
from app.models.user import User
from app.schemas.scans import (
    PromptRunDetailResponse,
    PromptRunSummaryResponse,
    ResponseSourceResponse,
    ScanCreateRequest,
    ScanDetailResponse,
    ScanListResponse,
    ScanSummaryResponse,
)
from app.services.audit_service import AuditService
from app.services.entitlement_service import EntitlementService
from app.services.scan_creation_service import ScanCreationService
from app.services.scan_query_service import ScanQueryService, ScanView
from app.services.scanning.dispatcher import CeleryScanDispatcher, ScanDispatcher
from app.services.workspace_auth_service import WorkspaceAuthorizationService

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/projects/{project_id}/scans",
    tags=["scans"],
)


def get_scan_dispatcher() -> ScanDispatcher:
    return CeleryScanDispatcher()


def get_scan_creation_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
    dispatcher: Annotated[ScanDispatcher, Depends(get_scan_dispatcher)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
) -> ScanCreationService:
    return ScanCreationService(
        entitlement_service._session,
        dispatcher,
        audit_service=audit,
    )


def get_scan_query_service(
    entitlement_service: Annotated[EntitlementService, Depends(get_entitlement_service)],
) -> ScanQueryService:
    return ScanQueryService(entitlement_service._session)


@router.post("", response_model=ScanSummaryResponse, status_code=status.HTTP_202_ACCEPTED)
def create_scan(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    request: ScanCreateRequest,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[ScanCreationService, Depends(get_scan_creation_service)],
    query_service: Annotated[ScanQueryService, Depends(get_scan_query_service)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ScanSummaryResponse:
    auth.require_role(workspace_id, user.id, WorkspaceRole.ADMIN)
    result = service.create_scan(
        workspace_id,
        project_id,
        request.scan_type,
        user.id,
        idempotency_key,
    )
    return _summary(query_service.get_scan(workspace_id, project_id, result.scan.id))


@router.get("", response_model=ScanListResponse)
def list_scans(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[ScanQueryService, Depends(get_scan_query_service)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ScanListResponse:
    auth.require_membership(workspace_id, user.id)
    items = service.list_scans(workspace_id, project_id, offset, limit)
    return ScanListResponse(items=[_summary(item) for item in items], offset=offset, limit=limit)


@router.get("/{scan_id}", response_model=ScanDetailResponse)
def get_scan(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[ScanQueryService, Depends(get_scan_query_service)],
) -> ScanDetailResponse:
    auth.require_membership(workspace_id, user.id)
    view = service.get_scan(workspace_id, project_id, scan_id)
    summary = _summary(view)
    runs = service.list_runs(workspace_id, project_id, scan_id)
    return ScanDetailResponse(**summary.model_dump(), runs=[_run_summary(run) for run in runs])


@router.get("/{scan_id}/runs", response_model=list[PromptRunSummaryResponse])
def list_prompt_runs(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[ScanQueryService, Depends(get_scan_query_service)],
) -> list[PromptRunSummaryResponse]:
    auth.require_membership(workspace_id, user.id)
    return [_run_summary(run) for run in service.list_runs(workspace_id, project_id, scan_id)]


@router.get("/{scan_id}/runs/{prompt_run_id}", response_model=PromptRunDetailResponse)
def get_prompt_run(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    scan_id: uuid.UUID,
    prompt_run_id: uuid.UUID,
    user: Annotated[User, Depends(require_authenticated_user)],
    auth: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
    service: Annotated[ScanQueryService, Depends(get_scan_query_service)],
) -> PromptRunDetailResponse:
    auth.require_membership(workspace_id, user.id)
    run = service.get_run(workspace_id, project_id, scan_id, prompt_run_id)
    summary = _run_summary(run)
    return PromptRunDetailResponse(
        **summary.model_dump(),
        sources=[ResponseSourceResponse.model_validate(source) for source in run.sources],
    )


def _summary(view: ScanView) -> ScanSummaryResponse:
    scan = view.scan
    return ScanSummaryResponse(
        id=scan.id,
        project_id=scan.project_id,
        prompt_set_id=scan.prompt_set_id,
        prompt_set_version=view.prompt_set_version,
        scan_type=scan.scan_type,
        status=scan.status,
        prompt_count=scan.prompt_count,
        provider_count=scan.provider_count,
        planned_ai_checks=scan.planned_ai_checks,
        successful_runs=scan.successful_runs,
        failed_runs=scan.failed_runs,
        providers=view.providers,
        created_at=scan.created_at,
        started_at=scan.started_at,
        completed_at=scan.completed_at,
    )


def _run_summary(run: PromptRun) -> PromptRunSummaryResponse:
    return PromptRunSummaryResponse.model_validate(run)
