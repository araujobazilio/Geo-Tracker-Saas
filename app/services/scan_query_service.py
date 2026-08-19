"""Tenant-scoped customer-safe Scan and PromptRun reads."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.enums import LLMProvider
from app.core.exceptions import NotFoundError
from app.models.prompt_set import PromptSet
from app.models.scan import PromptRun, Scan
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import PromptRunRepository, ScanRepository
from app.services.scanning.policy import PROVIDER_ORDER


@dataclass(frozen=True)
class ScanView:
    scan: Scan
    prompt_set_version: int
    providers: list[LLMProvider]


class ScanQueryService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._scans = ScanRepository(session)
        self._runs = PromptRunRepository(session)

    def list_scans(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        offset: int,
        limit: int,
    ) -> list[ScanView]:
        self._require_project(workspace_id, project_id)
        return [
            self._view(scan)
            for scan in self._scans.list_scoped(workspace_id, project_id, offset, limit)
        ]

    def get_scan(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, scan_id: uuid.UUID
    ) -> ScanView:
        self._require_project(workspace_id, project_id)
        scan = self._scans.get_scoped(workspace_id, project_id, scan_id)
        if scan is None:
            raise NotFoundError("Scan not found.")
        return self._view(scan)

    def list_runs(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, scan_id: uuid.UUID
    ) -> list[PromptRun]:
        self.get_scan(workspace_id, project_id, scan_id)
        return self._runs.list_by_scan(scan_id)

    def get_run(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        scan_id: uuid.UUID,
        prompt_run_id: uuid.UUID,
    ) -> PromptRun:
        self.get_scan(workspace_id, project_id, scan_id)
        run = self._runs.get_scoped(
            workspace_id,
            project_id,
            scan_id,
            prompt_run_id,
            include_sources=True,
        )
        if run is None:
            raise NotFoundError("PromptRun not found.")
        return run

    def _view(self, scan: Scan) -> ScanView:
        prompt_set = self._session.get(PromptSet, scan.prompt_set_id)
        if prompt_set is None:
            raise NotFoundError("Scan PromptSet not found.")
        configured = {run.provider for run in self._runs.list_by_scan(scan.id)}
        providers = [provider for provider in PROVIDER_ORDER if provider in configured]
        return ScanView(scan=scan, prompt_set_version=prompt_set.version, providers=providers)

    def _require_project(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> None:
        if self._projects.get_in_workspace(project_id, workspace_id) is None:
            raise NotFoundError("Project not found.")
