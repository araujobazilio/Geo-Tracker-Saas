"""Project provider service — manage enabled AI providers per project.

PUT replaces the entire enabled-provider set. Every enabled provider
MUST be allowed by EffectiveEntitlements.allowed_providers.

Provider changes do NOT increment prompt_input_revision (providers
don't affect prompt text).

Plan downgrade behavior: if a provider disappears from the plan, the
project configuration is NOT mutated. The API exposes configured/enabled
and allowed_by_plan separately. The future Scan Engine will use the
intersection of project enabled providers and effective allowed providers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.enums import LLMProvider
from app.core.exceptions import ConflictError, EntitlementDeniedError
from app.core.logging import get_logger
from app.models.tracking import ProjectProvider
from app.repositories.project_repository import ProjectRepository
from app.repositories.tracking_repository import ProjectProviderRepository
from app.services.entitlement_service import EntitlementService

logger = get_logger("app.project_provider")


@dataclass
class ProviderInfo:
    """Provider configuration info for API responses."""

    provider: LLMProvider
    enabled: bool
    allowed_by_plan: bool


class ProjectProviderService:
    """Manage per-project enabled AI providers."""

    def __init__(
        self,
        session: Session,
        entitlement_service: EntitlementService | None = None,
    ) -> None:
        self._session = session
        self._project_repo = ProjectRepository(session)
        self._provider_repo = ProjectProviderRepository(session)
        self._entitlement_service = entitlement_service or EntitlementService(session)

    def list_providers(self, workspace_id: uuid.UUID, project_id: uuid.UUID) -> list[ProviderInfo]:
        """List all configured providers with allowed_by_plan status."""
        project = self._project_repo.get_in_workspace(project_id, workspace_id)
        if project is None:
            raise ConflictError("Project not found.")

        configured = self._provider_repo.list_by_project(project.id)
        ent = self._entitlement_service.get_effective_entitlements(workspace_id)

        result: list[ProviderInfo] = []
        for pp in configured:
            result.append(
                ProviderInfo(
                    provider=pp.provider,
                    enabled=pp.enabled,
                    allowed_by_plan=pp.provider in ent.allowed_providers,
                )
            )
        return result

    def set_providers(
        self,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        providers: list[LLMProvider],
    ) -> list[ProjectProvider]:
        """Set the enabled provider set for a project (PUT replace).

        Every provider must be allowed by the workspace's plan.
        Does NOT increment prompt_input_revision.
        """
        try:
            project = self._project_repo.get_in_workspace(project_id, workspace_id)
            if project is None:
                raise ConflictError("Project not found.")

            # Validate all providers are allowed by plan.
            for provider in providers:
                self._entitlement_service.require_provider(workspace_id, provider)

            # Delete existing and create new (PUT replace semantics).
            existing = self._provider_repo.list_by_project(project.id)
            for pp in existing:
                self._session.delete(pp)
            self._session.flush()

            created: list[ProjectProvider] = []
            for provider in providers:
                pp = ProjectProvider(
                    project_id=project.id,
                    provider=provider,
                    enabled=True,
                )
                self._provider_repo.create(pp)
                created.append(pp)

            self._session.commit()
            return created

        except (ConflictError, EntitlementDeniedError):
            self._session.rollback()
            raise
