"""Development-only seed script.

Populates the local database with a representative demo dataset:
- User: demo@example.com
- Workspace: Demo Agency (AGENCY)
- Project: Acme Email Marketing
- Keywords, competitors, providers, prompts (NON_BRANDED / BRANDED / COMPETITOR)
- BillingAccount (ADMIN source), AppSumoLicense (ACTIVE)

This script MUST NEVER run automatically in production. It is guarded by
the DEV_SEED_ENABLED setting and refuses to run outside development/test.
"""

from __future__ import annotations

import sys

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.enums import (
    AppSumoLicenseStatus,
    BillingAccountStatus,
    BillingSource,
    CompetitorSource,
    FunnelStage,
    LLMProvider,
    ProjectStatus,
    PromptType,
    WorkspaceRole,
    WorkspaceType,
)
from app.core.security import hash_password
from app.db.session import get_session_factory
from app.models import (
    AppSumoLicense,
    BillingAccount,
    Competitor,
    Project,
    ProjectKeyword,
    ProjectProvider,
    Prompt,
    User,
    Workspace,
    WorkspaceMember,
)


def _seed(session: Session) -> None:
    user = User(
        email="demo@example.com",
        password_hash=hash_password("demo-password-not-for-production"),
        is_active=True,
        is_admin=False,
    )
    session.add(user)
    session.flush()

    workspace = Workspace(name="Demo Agency", workspace_type=WorkspaceType.AGENCY)
    session.add(workspace)
    session.flush()

    membership = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role=WorkspaceRole.OWNER,
    )
    session.add(membership)

    project = Project(
        workspace_id=workspace.id,
        name="Acme Email Marketing",
        domain="acme.example",
        brand_name="Acme",
        brand_aliases=["Acme Inc", "Acme Email"],
        industry="Email Marketing",
        target_country="US",
        target_language="en",
        target_audience="Small Businesses",
        status=ProjectStatus.ACTIVE,
    )
    session.add(project)
    session.flush()

    keyword_texts = [
        ("best email marketing software", FunnelStage.PURCHASE),
        ("email automation platform", FunnelStage.CONSIDERATION),
        ("email marketing software for small business", FunnelStage.PURCHASE),
    ]
    keywords: list[ProjectKeyword] = []
    for text, stage in keyword_texts:
        kw = ProjectKeyword(
            project_id=project.id,
            text=text,
            funnel_stage=stage,
            active=True,
        )
        session.add(kw)
        keywords.append(kw)
    session.flush()

    competitors = [
        Competitor(
            project_id=project.id,
            name="Mailchimp",
            domain="mailchimp.com",
            aliases=["MailChimp"],
            source=CompetitorSource.USER_DEFINED,
            active=True,
        ),
        Competitor(
            project_id=project.id,
            name="Brevo",
            domain="brevo.com",
            source=CompetitorSource.USER_DEFINED,
            active=True,
        ),
        Competitor(
            project_id=project.id,
            name="ActiveCampaign",
            domain="activecampaign.com",
            source=CompetitorSource.USER_DEFINED,
            active=True,
        ),
    ]
    for c in competitors:
        session.add(c)

    for provider in LLMProvider:
        session.add(
            ProjectProvider(
                project_id=project.id,
                provider=provider,
                enabled=True,
            )
        )

    # Example prompts demonstrating the three prompt types.
    nb_kw = keywords[0]
    session.add(
        Prompt(
            project_keyword_id=nb_kw.id,
            text="What is the best email marketing software for a small business?",
            prompt_set_version=1,
            prompt_type=PromptType.NON_BRANDED,
            funnel_stage=FunnelStage.PURCHASE,
            target_country="US",
            target_language="en",
            commercial_intent=True,
            active=True,
        )
    )
    session.add(
        Prompt(
            project_keyword_id=nb_kw.id,
            text="Is Acme a good email marketing software?",
            prompt_set_version=1,
            prompt_type=PromptType.BRANDED,
            funnel_stage=FunnelStage.CONSIDERATION,
            target_country="US",
            target_language="en",
            commercial_intent=True,
            active=True,
        )
    )
    session.add(
        Prompt(
            project_keyword_id=nb_kw.id,
            text="Acme vs Mailchimp for a small company",
            prompt_set_version=1,
            prompt_type=PromptType.COMPETITOR,
            funnel_stage=FunnelStage.PURCHASE,
            target_country="US",
            target_language="en",
            commercial_intent=True,
            active=True,
        )
    )

    billing = BillingAccount(
        workspace_id=workspace.id,
        source=BillingSource.ADMIN,
        status=BillingAccountStatus.ACTIVE,
        plan_code="dev_seed",
    )
    session.add(billing)
    session.flush()

    session.add(
        AppSumoLicense(
            workspace_id=workspace.id,
            billing_account_id=billing.id,
            external_license_id="dev-seed-license-0001",
            appsumo_plan="tier_2_pro",
            status=AppSumoLicenseStatus.ACTIVE,
        )
    )

    session.commit()


def main() -> int:
    settings = get_settings()
    if settings.is_production:
        print("Refusing to seed: APP_ENV is production.", file=sys.stderr)
        return 1
    if not settings.dev_seed_enabled:
        print(
            "Refusing to seed: DEV_SEED_ENABLED is not true. "
            "Set DEV_SEED_ENABLED=true to allow development seeding.",
            file=sys.stderr,
        )
        return 1

    factory = get_session_factory()
    with factory() as session:
        _seed(session)
    print("Seed completed: demo@example.com / demo-password-not-for-production")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
