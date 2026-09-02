#!/usr/bin/env python3
"""Seed internal PlanDefinitions for production deployment.

Idempotent: existing plans are upserted (updated in place). New plans
are created. No plan is ever deleted by this script.

PlanDefinition is entitlement configuration — NOT commercial pricing.
External commercial pricing integration remains Phase 14.

Usage:
    python -m scripts.seed_plans
"""

from __future__ import annotations

import sys

EXIT_OK = 0
EXIT_FAIL = 1

# Internal plan definitions for closed beta.
# These are entitlement configurations, NOT commercial prices.
_PLANS = [
    {
        "code": "beta_internal",
        "name": "Beta (Internal)",
        "description": "Closed beta complimentary access plan.",
        "is_active": True,
        "max_projects": 10,
        "max_keywords_per_project": 50,
        "max_competitors_per_project": 20,
        "max_team_members": 10,
        "monthly_ai_checks": 200,
        "min_scheduled_scan_interval_hours": 24,
        "confidence_scans_enabled": True,
        "verification_scans_enabled": True,
        "white_label_reports": False,
        "exports_enabled": False,
        "agency_dashboard": False,
        "integrations_enabled": False,
        "byok_enabled": False,
        "providers": ["OPENAI", "ANTHROPIC", "GOOGLE", "PERPLEXITY"],
    },
    {
        "code": "dev_internal",
        "name": "Development (Internal)",
        "description": "Development and testing plan.",
        "is_active": True,
        "max_projects": 100,
        "max_keywords_per_project": 100,
        "max_competitors_per_project": 50,
        "max_team_members": 50,
        "monthly_ai_checks": 10000,
        "min_scheduled_scan_interval_hours": 1,
        "confidence_scans_enabled": True,
        "verification_scans_enabled": True,
        "white_label_reports": True,
        "exports_enabled": True,
        "agency_dashboard": True,
        "integrations_enabled": True,
        "byok_enabled": False,
        "providers": ["OPENAI", "ANTHROPIC", "GOOGLE", "PERPLEXITY"],
    },
]


def main() -> int:
    from sqlalchemy import select

    from app.core.enums import LLMProvider
    from app.db.session import get_session_factory
    from app.models.plan_definition import PlanDefinition
    from app.models.plan_provider import PlanProvider

    factory = get_session_factory()
    created = 0
    updated = 0

    with factory() as session:
        for plan_data in _PLANS:
            existing = session.execute(
                select(PlanDefinition).where(PlanDefinition.code == plan_data["code"])
            ).scalar_one_or_none()

            providers = plan_data.pop("providers", [])

            if existing is None:
                plan = PlanDefinition(**plan_data)
                session.add(plan)
                session.flush()
                created += 1
                print(f"Created plan: {plan.code}")
            else:
                for key, value in plan_data.items():
                    setattr(existing, key, value)
                plan = existing
                updated += 1
                print(f"Updated plan: {plan.code}")

            # Upsert providers.
            existing_providers = {
                pp.provider
                for pp in session.execute(
                    select(PlanProvider).where(PlanProvider.plan_id == plan.id)
                )
                .scalars()
                .all()
            }

            for provider_str in providers:
                provider = LLMProvider(provider_str)
                if provider not in existing_providers:
                    session.add(PlanProvider(plan_id=plan.id, provider=provider))

        session.commit()

    print(f"\nSeeding complete: {created} created, {updated} updated.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
