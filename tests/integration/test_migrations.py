"""Integration tests for Alembic migrations (schema source of truth).

Verifies that a fresh database can be migrated from zero to head via the
real Alembic migration path, and that the expected tables exist.
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

# Expected tables after migrating to head (Phase 1 + hardening migration).
# Does not include alembic_version (Alembic's own bookkeeping table).
EXPECTED_TABLES = {
    "appsumo_licenses",
    "audit_logs",
    "billing_accounts",
    "competitors",
    "project_keywords",
    "project_providers",
    "projects",
    "prompts",
    "provider_webhook_events",
    "usage_events",
    "users",
    "workspace_members",
    "workspaces",
}


def _alembic_config(url: str) -> AlembicConfig:
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    cfg = AlembicConfig(os.path.join(project_root, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", os.path.join(project_root, "alembic"))
    return cfg


@pytest.mark.integration
def test_fresh_database_migrates_to_head(db_session) -> None:  # type: ignore[no-untyped-def]
    """The test database (already migrated by the conftest) must contain
    all expected tables. This indirectly verifies the migration path.
    """
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url, poolclass=NullPool)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
    finally:
        engine.dispose()

    missing = EXPECTED_TABLES - tables
    assert not missing, f"Missing tables after migration: {missing}"
    # alembic_version must also exist.
    assert "alembic_version" in tables


@pytest.mark.integration
def test_usage_events_check_constraints_exist(db_session) -> None:  # type: ignore[no-untyped-def]
    """The non-negative CHECK constraints must be present on usage_events."""
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'usage_events'::regclass AND contype = 'c' "
                    "ORDER BY conname"
                )
            )
            check_names = {r[0] for r in rows}
    finally:
        engine.dispose()

    expected = {
        "ck_usage_events_ai_checks_non_negative",
        "ck_usage_events_input_tokens_non_negative",
        "ck_usage_events_output_tokens_non_negative",
        "ck_usage_events_total_tokens_non_negative",
        "ck_usage_events_cost_usd_non_negative",
    }
    missing = expected - check_names
    assert not missing, f"Missing CHECK constraints: {missing}"


@pytest.mark.integration
def test_appsumo_license_unique_constraint_exists(db_session) -> None:  # type: ignore[no-untyped-def]
    """The unique constraint on external_license_id must be present."""
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'appsumo_licenses'::regclass AND contype = 'u' "
                    "ORDER BY conname"
                )
            )
            unique_names = {r[0] for r in rows}
    finally:
        engine.dispose()

    assert "uq_appsumo_licenses_external_license_id" in unique_names


@pytest.mark.integration
def test_migration_drift_check(db_session) -> None:  # type: ignore[no-untyped-def]
    """Run `alembic check` to detect ORM/migration drift.

    `alembic check` (added in Alembic 1.9+) compares the current database
    state against the autogenerate-diffable metadata. If the ORM models
    have drifted from the migrations, this test fails.
    """
    url = os.environ["DATABASE_URL"]
    cfg = _alembic_config(url)
    # `alembic check` raises if there is drift.
    command.check(cfg)
