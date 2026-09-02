#!/usr/bin/env python3
"""Production configuration readiness check.

Checks (WITHOUT making any paid provider API calls):
  - production Settings load (fail-fast validation)
  - database connectivity
  - Redis connectivity
  - configured provider/model pairs (structural presence only)
  - SMTP settings if EMAIL_ENABLED
  - PlanDefinitions exist in the database
  - Alembic migration head

Does NOT call OpenAI, Anthropic, Google, or Perplexity.
Does NOT send emails.

Usage:
    python -m scripts.check_production_config
    python -m scripts.check_production_config --database-url ...
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

# Exit codes: 0 = all checks passed, 1 = one or more checks failed.
EXIT_OK = 0
EXIT_FAIL = 1


def _check_settings() -> bool:
    """Verify that production Settings load without error."""
    print("Checking production settings...", end=" ")
    try:
        from app.config import get_settings

        settings = get_settings()
        if not settings.is_production:
            print(f"FAIL (APP_ENV={settings.app_env}, expected production)")
            return False
        print("OK")
        return True
    except Exception as exc:
        print(f"FAIL ({exc})")
        return False


def _check_database() -> bool:
    """Verify database connectivity."""
    print("Checking database connectivity...", end=" ")
    try:
        from app.db.session import check_database

        if check_database():
            print("OK")
            return True
        print("FAIL (SELECT 1 failed)")
        return False
    except Exception as exc:
        print(f"FAIL ({exc})")
        return False


def _check_redis() -> bool:
    """Verify Redis connectivity."""
    print("Checking Redis connectivity...", end=" ")
    try:
        from app.db.redis import check_redis

        if check_redis():
            print("OK")
            return True
        print("FAIL (PING failed)")
        return False
    except Exception as exc:
        print(f"FAIL ({exc})")
        return False


def _check_providers() -> bool:
    """Check that configured provider/model pairs are structurally present.

    Does NOT make API calls. Only checks that keys and model names are set.
    A missing provider is NOT a failure — it just means that provider is disabled.
    """
    print("Checking provider configuration (structural only)...", end=" ")
    try:
        from app.config import get_settings

        settings = get_settings()
        providers = {
            "OPENAI": (settings.openai_api_key.get_secret_value(), settings.openai_scan_model),
            "ANTHROPIC": (
                settings.anthropic_api_key.get_secret_value(),
                settings.anthropic_scan_model,
            ),
            "GOOGLE": (settings.google_api_key.get_secret_value(), settings.google_scan_model),
            "PERPLEXITY": (
                settings.perplexity_api_key.get_secret_value(),
                settings.perplexity_scan_model,
            ),
        }
        configured = []
        incomplete = []
        for name, (key, model) in providers.items():
            if key and model:
                configured.append(name)
            elif key or model:
                incomplete.append(
                    f"{name} (key={'set' if key else 'missing'}, model={'set' if model else 'missing'})"
                )

        if incomplete:
            print(f"WARN (incomplete: {', '.join(incomplete)})")
        else:
            print(f"OK (configured: {', '.join(configured) if configured else 'none'})")
        # Incomplete providers are warnings, not failures.
        return True
    except Exception as exc:
        print(f"FAIL ({exc})")
        return False


def _check_smtp() -> bool:
    """Check SMTP settings if EMAIL_ENABLED is true."""
    print("Checking SMTP configuration...", end=" ")
    try:
        from app.config import get_settings

        settings = get_settings()
        if not settings.email_enabled:
            print("OK (email disabled)")
            return True
        if not settings.smtp_host:
            print("FAIL (EMAIL_ENABLED=true but SMTP_HOST is empty)")
            return False
        if not settings.email_from_address:
            print("FAIL (EMAIL_ENABLED=true but EMAIL_FROM_ADDRESS is empty)")
            return False
        print(f"OK (host={settings.smtp_host}, port={settings.smtp_port})")
        return True
    except Exception as exc:
        print(f"FAIL ({exc})")
        return False


def _check_plan_definitions() -> bool:
    """Check that at least one active PlanDefinition exists."""
    print("Checking PlanDefinitions...", end=" ")
    try:
        from sqlalchemy import func, select

        from app.db.session import get_session_factory
        from app.models.plan_definition import PlanDefinition

        factory = get_session_factory()
        with factory() as session:
            count = session.execute(
                select(func.count())
                .select_from(PlanDefinition)
                .where(PlanDefinition.is_active.is_(True))
            ).scalar_one()
        if count > 0:
            print(f"OK ({count} active plan(s))")
            return True
        print("FAIL (no active PlanDefinitions — run scripts/seed_plans.py)")
        return False
    except Exception as exc:
        print(f"FAIL ({exc})")
        return False


def _check_migration_head() -> bool:
    """Check that the database is at the Alembic migration head."""
    print("Checking Alembic migration head...", end=" ")
    try:
        import os

        from alembic.config import Config as AlembicConfig
        from alembic.runtime.migration import MigrationContext

        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        cfg = AlembicConfig(os.path.join(project_root, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(project_root, "alembic"))

        from app.db.session import get_engine

        engine = get_engine()
        with engine.connect() as conn:
            mc = MigrationContext.configure(conn)
            current_rev = mc.get_current_revision()

        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(cfg)
        head_rev = script.get_current_head()

        if current_rev == head_rev:
            print(f"OK (at head: {head_rev})")
            return True
        print(f"FAIL (current={current_rev}, head={head_rev})")
        return False
    except Exception as exc:
        print(f"FAIL ({exc})")
        return False


def main(argv: Sequence[str] | None = None) -> int:
    """Run all production config checks."""
    print("=" * 60)
    print("GEO Tracker — Production Configuration Check")
    print("=" * 60)
    print()

    checks = [
        _check_settings,
        _check_database,
        _check_redis,
        _check_providers,
        _check_smtp,
        _check_plan_definitions,
        _check_migration_head,
    ]

    all_passed = True
    for check in checks:
        if not check():
            all_passed = False

    print()
    print("=" * 60)
    if all_passed:
        print("All checks PASSED.")
        return EXIT_OK
    print("One or more checks FAILED.")
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
