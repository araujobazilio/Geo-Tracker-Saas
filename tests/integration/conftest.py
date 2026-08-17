"""Integration test configuration.

Integration tests run against a REAL PostgreSQL instance. They use a
DEDICATED test database (`geo_tracker_test`), never the development
database (`geo_tracker`).

The test schema is prepared via the REAL Alembic migration path
(`alembic upgrade head`), NOT via `Base.metadata.create_all()`. This
ensures migrations are the canonical schema source of truth and that
model/migration drift is detectable.

Tests use a per-test transaction that is rolled back, so they are
isolated and never leave persistent state.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

# Force test environment before any app imports.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")

# Integration tests use a DEDICATED test database, never the dev DB.
# Default to the Docker Compose test database on the remapped host port.
TEST_DATABASE_URL = os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)

# Alembic needs a sync psycopg URL (no +asyncpg). The test URL already is.
ALEMBIC_URL = TEST_DATABASE_URL

# Module-level: prepare the schema once via Alembic.
_SCHEMA_READY = False


def _database_available() -> bool:
    try:
        engine = create_engine(ALEMBIC_URL, poolclass=NullPool)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


DB_AVAILABLE = _database_available()


def _ensure_schema() -> None:
    """Run Alembic migrations to head on the test database (once per session)."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    # Locate alembic.ini relative to the project root.
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    alembic_ini = os.path.join(project_root, "alembic.ini")
    cfg = AlembicConfig(alembic_ini)
    cfg.set_main_option("sqlalchemy.url", ALEMBIC_URL)
    # Point script_location at the project's alembic dir explicitly.
    cfg.set_main_option("script_location", os.path.join(project_root, "alembic"))

    # Drop + recreate the test schema to guarantee a clean, migrated state.
    # This is safe because geo_tracker_test is a dedicated test database.
    engine = create_engine(ALEMBIC_URL, poolclass=NullPool)
    with engine.connect() as conn:
        # Drop all tables (cascade) to reset to a known empty state.
        conn.execute(
            text(
                "DROP SCHEMA IF EXISTS public CASCADE; "
                "CREATE SCHEMA public; "
                "GRANT ALL ON SCHEMA public TO geo_tracker; "
                "GRANT ALL ON SCHEMA public TO public;"
            )
        )
        conn.commit()
    engine.dispose()

    # Apply migrations via the real Alembic path.
    command.upgrade(cfg, "head")
    _SCHEMA_READY = True


if DB_AVAILABLE:
    _ensure_schema()


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """Yield a session backed by a single connection with a rollback.

    Each test runs inside a transaction that is rolled back at the end,
    so tests are isolated and never pollute the test database.
    """
    if not DB_AVAILABLE:
        pytest.skip("PostgreSQL not available for integration tests")

    from app.db.session import reset_engine

    reset_engine()
    engine: Engine = create_engine(ALEMBIC_URL, poolclass=NullPool, future=True)

    connection = engine.connect()
    trans = connection.begin()
    factory = sessionmaker(
        bind=connection, autoflush=False, autocommit=False, expire_on_commit=False
    )
    session = factory()

    try:
        yield session
    finally:
        session.close()
        if trans.is_active:
            trans.rollback()
        connection.close()
        engine.dispose()
