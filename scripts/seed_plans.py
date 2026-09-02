#!/usr/bin/env python3
"""Seed internal PlanDefinitions for production deployment.

Idempotent: existing plans are upserted (updated in place). New plans
are created. PlanProvider rows are reconciled to the exact intended set
(missing providers are added, stale providers are removed).

PlanDefinition is entitlement configuration — NOT commercial pricing.
External commercial pricing integration remains Phase 14.

Production seeding creates ONLY the beta_internal plan. The dev_internal
plan is NOT seeded by this command in production — it belongs to dev seed
logic only.

Usage:
    python -m scripts.seed_plans
    python -m scripts.seed_plans --beta-providers OPENAI,ANTHROPIC
    python -m scripts.seed_plans --include-dev  # dev environments only

Environment:
    BETA_ALLOWED_PROVIDERS=OPENAI,ANTHROPIC  (comma-separated)
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
from typing import Any

EXIT_OK = 0
EXIT_FAIL = 1

# Production plan definition for closed beta.
# This is entitlement configuration, NOT commercial pricing.
_BETA_PLAN: dict[str, Any] = {
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
    "providers": ["OPENAI", "ANTHROPIC"],
}

# Development plan — only seeded with --include-dev.
_DEV_PLAN: dict[str, Any] = {
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
}


def _validate_providers(provider_strs: list[str]) -> list[str]:
    """Validate that all provider names are known LLMProvider enum values."""
    from app.core.enums import LLMProvider

    valid = {p.value for p in LLMProvider}
    for name in provider_strs:
        if name not in valid:
            print(f"ERROR: Unknown provider '{name}'. Valid: {sorted(valid)}")
            sys.exit(EXIT_FAIL)
    return provider_strs


def _reconcile_providers(
    session: Any,
    plan_id: Any,
    desired_provider_strs: list[str],
) -> tuple[int, int]:
    """Reconcile PlanProvider rows to the exact desired set.

    Returns (added_count, removed_count).
    """
    from sqlalchemy import select

    from app.core.enums import LLMProvider
    from app.models.plan_provider import PlanProvider

    desired = {LLMProvider(p) for p in desired_provider_strs}

    existing_rows = (
        session.execute(select(PlanProvider).where(PlanProvider.plan_id == plan_id)).scalars().all()
    )
    existing = {pp.provider for pp in existing_rows}

    added = 0
    removed = 0

    # Insert missing.
    for provider in desired - existing:
        session.add(PlanProvider(plan_id=plan_id, provider=provider))
        added += 1

    # Remove stale.
    for provider in existing - desired:
        for row in existing_rows:
            if row.provider == provider:
                session.delete(row)
                removed += 1

    return added, removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed PlanDefinitions for production.")
    parser.add_argument(
        "--beta-providers",
        default=None,
        help="Comma-separated provider list for beta_internal (e.g. OPENAI,ANTHROPIC). "
        "Overrides BETA_ALLOWED_PROVIDERS env var.",
    )
    parser.add_argument(
        "--include-dev",
        action="store_true",
        default=False,
        help="Also seed dev_internal plan (dev environments only).",
    )
    args = parser.parse_args(argv)

    # Determine beta providers: CLI > env > default.
    if args.beta_providers:
        beta_providers = [p.strip() for p in args.beta_providers.split(",") if p.strip()]
    elif env_providers := os.environ.get("BETA_ALLOWED_PROVIDERS", ""):
        beta_providers = [p.strip() for p in env_providers.split(",") if p.strip()]
    else:
        beta_providers = list(_BETA_PLAN["providers"])

    beta_providers = _validate_providers(beta_providers)

    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.plan_definition import PlanDefinition

    # Build the list of plans to seed (copy to avoid mutating module-level data).
    plans_to_seed: list[dict[str, Any]] = []
    beta_plan = copy.deepcopy(_BETA_PLAN)
    beta_plan["providers"] = beta_providers
    plans_to_seed.append(beta_plan)

    if args.include_dev:
        plans_to_seed.append(copy.deepcopy(_DEV_PLAN))

    factory = get_session_factory()
    created = 0
    updated = 0
    providers_added = 0
    providers_removed = 0

    with factory() as session:
        for plan_data in plans_to_seed:
            # Extract providers without mutating the original dict.
            providers = plan_data["providers"]
            plan_fields = {k: v for k, v in plan_data.items() if k != "providers"}

            existing = session.execute(
                select(PlanDefinition).where(PlanDefinition.code == plan_data["code"])
            ).scalar_one_or_none()

            if existing is None:
                plan = PlanDefinition(**plan_fields)
                session.add(plan)
                session.flush()
                created += 1
                print(f"Created plan: {plan.code}")
            else:
                for key, value in plan_fields.items():
                    setattr(existing, key, value)
                plan = existing
                updated += 1
                print(f"Updated plan: {plan.code}")

            # Reconcile providers exactly (add missing, remove stale).
            added, removed = _reconcile_providers(session, plan.id, providers)
            providers_added += added
            providers_removed += removed
            if added or removed:
                print(
                    f"  Providers reconciled: +{added} added, -{removed} removed "
                    f"→ {', '.join(providers)}"
                )

        session.commit()

    print(
        f"\nSeeding complete: {created} created, {updated} updated, "
        f"{providers_added} providers added, {providers_removed} providers removed."
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
