"""Entitlement service — resolves effective entitlements for a workspace.

Source-independent: AppSumo, Stripe, and Admin grants all map to a
plan_code on the BillingAccount, which resolves to a PlanDefinition.
Entitlement rules are NOT duplicated across billing integrations.

Fail-safe: if there is no BillingAccount, no primary billing account,
inactive/canceled status, missing plan_code, unknown plan_code, or
inactive PlanDefinition, the service returns UNENTITLED.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.entitlements import EffectiveEntitlements
from app.core.enums import BillingAccountStatus, LLMProvider
from app.core.exceptions import EntitlementDeniedError
from app.core.logging import get_logger
from app.repositories.billing_repository import BillingAccountRepository
from app.repositories.plan_repository import PlanProviderRepository, PlanRepository
from app.repositories.workspace_repository import WorkspaceMemberRepository

logger = get_logger("app.entitlements")

# Billing account statuses eligible for entitlement resolution.
_ELIGIBLE_STATUSES = frozenset(
    {
        BillingAccountStatus.ACTIVE,
        BillingAccountStatus.TRIALING,
    }
)


class EntitlementService:
    """Resolve effective entitlements for a workspace.

    This is the single point of entitlement resolution. Routers and
    services consume EffectiveEntitlements, never billing tables.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._billing_repo = BillingAccountRepository(session)
        self._plan_repo = PlanRepository(session)
        self._provider_repo = PlanProviderRepository(session)
        self._member_repo = WorkspaceMemberRepository(session)

    def get_effective_entitlements(self, workspace_id: uuid.UUID) -> EffectiveEntitlements:
        """Resolve the effective entitlements for a workspace.

        Returns UNENTITLED if any step of the resolution chain fails.
        Never raises — always returns a conservative snapshot.
        """
        billing = self._billing_repo.get_primary(workspace_id)
        if billing is None:
            logger.debug("entitlement_unentitled_no_billing", workspace_id=str(workspace_id))
            return EffectiveEntitlements.unentitled(workspace_id)

        if billing.status not in _ELIGIBLE_STATUSES:
            logger.debug(
                "entitlement_unentitled_status",
                workspace_id=str(workspace_id),
                status=billing.status,
            )
            return EffectiveEntitlements.unentitled(workspace_id)

        if not billing.plan_code:
            logger.debug(
                "entitlement_unentitled_no_plan_code",
                workspace_id=str(workspace_id),
            )
            return EffectiveEntitlements.unentitled(workspace_id)

        plan = self._plan_repo.get_by_code(billing.plan_code)
        if plan is None:
            logger.debug(
                "entitlement_unentitled_unknown_plan",
                workspace_id=str(workspace_id),
                plan_code=billing.plan_code,
            )
            return EffectiveEntitlements.unentitled(workspace_id)

        if not plan.is_active:
            logger.debug(
                "entitlement_unentitled_inactive_plan",
                workspace_id=str(workspace_id),
                plan_code=billing.plan_code,
            )
            return EffectiveEntitlements.unentitled(workspace_id)

        # Resolve allowed providers from the relational association.
        provider_rows = self._provider_repo.list_for_plan(plan.id)
        allowed = frozenset(LLMProvider(p.provider) for p in provider_rows)

        return EffectiveEntitlements(
            workspace_id=workspace_id,
            plan_code=plan.code,
            billing_source=billing.source,
            max_projects=plan.max_projects,
            max_keywords_per_project=plan.max_keywords_per_project,
            max_competitors_per_project=plan.max_competitors_per_project,
            max_team_members=plan.max_team_members,
            monthly_ai_checks=plan.monthly_ai_checks,
            allowed_providers=allowed,
            min_scheduled_scan_interval_hours=plan.min_scheduled_scan_interval_hours,
            confidence_scans_enabled=plan.confidence_scans_enabled,
            verification_scans_enabled=plan.verification_scans_enabled,
            white_label_reports=plan.white_label_reports,
            exports_enabled=plan.exports_enabled,
            agency_dashboard=plan.agency_dashboard,
            integrations_enabled=plan.integrations_enabled,
            byok_enabled=plan.byok_enabled,
        )

    def is_provider_allowed(self, workspace_id: uuid.UUID, provider: LLMProvider) -> bool:
        """Check if a provider is allowed for the workspace's plan."""
        ent = self.get_effective_entitlements(workspace_id)
        return provider in ent.allowed_providers

    def require_provider(self, workspace_id: uuid.UUID, provider: LLMProvider) -> None:
        """Raise EntitlementDeniedError if the provider is not allowed."""
        if not self.is_provider_allowed(workspace_id, provider):
            raise EntitlementDeniedError(
                f"Provider '{provider.value}' is not available on your plan."
            )

    def require_feature(self, workspace_id: uuid.UUID, feature: str) -> None:
        """Raise EntitlementDeniedError if a feature flag is off.

        `feature` must be one of: confidence_scans, verification_scans,
        white_label_reports, exports, agency_dashboard, integrations, byok.
        """
        ent = self.get_effective_entitlements(workspace_id)
        flag_map = {
            "confidence_scans": ent.confidence_scans_enabled,
            "verification_scans": ent.verification_scans_enabled,
            "white_label_reports": ent.white_label_reports,
            "exports": ent.exports_enabled,
            "agency_dashboard": ent.agency_dashboard,
            "integrations": ent.integrations_enabled,
            "byok": ent.byok_enabled,
        }
        if feature not in flag_map:
            raise EntitlementDeniedError(f"Unknown feature: {feature}")
        if not flag_map[feature]:
            raise EntitlementDeniedError(f"Feature '{feature}' is not available on your plan.")

    def require_project_capacity(self, workspace_id: uuid.UUID, current_count: int) -> None:
        """Raise QuotaExceededError if workspace cannot add another project."""
        from app.core.exceptions import QuotaExceededError

        ent = self.get_effective_entitlements(workspace_id)
        if current_count >= ent.max_projects:
            raise QuotaExceededError(
                f"Project limit reached ({ent.max_projects}). "
                f"Upgrade your plan to create more projects."
            )

    def require_keyword_capacity(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, current_count: int
    ) -> None:
        """Raise QuotaExceededError if project cannot add another keyword.

        This is the add-one-more check: current_count is the active count
        BEFORE adding. If current_count >= max, adding one more would exceed.
        """
        from app.core.exceptions import QuotaExceededError

        ent = self.get_effective_entitlements(workspace_id)
        if current_count >= ent.max_keywords_per_project:
            raise QuotaExceededError(
                f"Keyword limit reached ({ent.max_keywords_per_project}) " f"for this project."
            )

    def require_keyword_total_within_limit(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, total_count: int
    ) -> None:
        """Raise QuotaExceededError if total_count exceeds the keyword limit.

        This is the total check: total_count is the FINAL active count
        (e.g. after onboarding inserts). total_count == max is allowed;
        only total_count > max is rejected.
        """
        from app.core.exceptions import QuotaExceededError

        ent = self.get_effective_entitlements(workspace_id)
        if total_count > ent.max_keywords_per_project:
            raise QuotaExceededError(
                f"Keyword limit exceeded ({total_count} > {ent.max_keywords_per_project}) "
                f"for this project."
            )

    def require_competitor_capacity(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, current_count: int
    ) -> None:
        """Raise QuotaExceededError if project cannot add another competitor.

        This is the add-one-more check: current_count is the active count
        BEFORE adding. If current_count >= max, adding one more would exceed.
        """
        from app.core.exceptions import QuotaExceededError

        ent = self.get_effective_entitlements(workspace_id)
        if current_count >= ent.max_competitors_per_project:
            raise QuotaExceededError(
                f"Competitor limit reached ({ent.max_competitors_per_project}) "
                f"for this project."
            )

    def require_competitor_total_within_limit(
        self, workspace_id: uuid.UUID, project_id: uuid.UUID, total_count: int
    ) -> None:
        """Raise QuotaExceededError if total_count exceeds the competitor limit.

        This is the total check: total_count is the FINAL active count
        (e.g. after onboarding inserts). total_count == max is allowed;
        only total_count > max is rejected.
        """
        from app.core.exceptions import QuotaExceededError

        ent = self.get_effective_entitlements(workspace_id)
        if total_count > ent.max_competitors_per_project:
            raise QuotaExceededError(
                f"Competitor limit exceeded ({total_count} > {ent.max_competitors_per_project}) "
                f"for this project."
            )

    def require_team_member_capacity(self, workspace_id: uuid.UUID) -> None:
        """Raise QuotaExceededError if workspace cannot add another member.

        OWNER is included in the count.
        """
        from app.core.exceptions import QuotaExceededError

        ent = self.get_effective_entitlements(workspace_id)
        current = len(self._member_repo.list_members(workspace_id))
        if current >= ent.max_team_members:
            raise QuotaExceededError(f"Team member limit reached ({ent.max_team_members}).")
