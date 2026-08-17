"""Integration test configuration.

Integration tests run against a REAL PostgreSQL instance. They use a
DEDICATED test database (`geo_tracker_test`), never the development
database (`geo_tracker`).

The test database URL is taken from the `TEST_DATABASE_URL` environment
variable (NOT from the application's `DATABASE_URL`), so a developer's
existing `DATABASE_URL` pointing at the dev/prod database can NEVER
cause the test suite to destroy it.

The test schema is prepared via the REAL Alembic migration path
(`alembic upgrade head`), NOT via `Base.metadata.create_all()`. This
ensures migrations are the canonical schema source of truth and that
model/migration drift is detectable.

Destructive schema reset is performed inside a session-scoped fixture
(never at import time) and is guarded by `assert_test_database_safe`,
which requires BOTH:
  - the database name to be exactly `geo_tracker_test`, AND
  - `APP_ENV` to be exactly `test`.

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

from tests.integration.db_safety import assert_test_database_safe

# Force test environment before any app imports.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")

# Integration tests use a DEDICATED test database, addressed via
# TEST_DATABASE_URL (NOT the application's DATABASE_URL). This prevents
# an existing DATABASE_URL pointing at the dev/prod DB from being used
# for destructive test setup.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test",
)

# Make the application / Alembic use the validated test database.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def _database_available(url: str) -> bool:
    try:
        engine = create_engine(url, poolclass=NullPool)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


DB_AVAILABLE = _database_available(TEST_DATABASE_URL)


def _alembic_config(url: str) -> AlembicConfig:
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    cfg = AlembicConfig(os.path.join(project_root, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", os.path.join(project_root, "alembic"))
    return cfg


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """Return the validated test database URL.

    Fails fast (before any DB operation) if the URL or environment is
    unsafe for destructive test setup.
    """
    assert_test_database_safe(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def prepared_test_db(test_db_url: str) -> str:
    """Session-scoped: reset the test schema and apply Alembic migrations.

    This is the ONLY place where a destructive schema reset occurs.
    It runs once per test session, after the safety guard has validated
    the target database.
    """
    if not DB_AVAILABLE:
        pytest.skip("PostgreSQL not available for integration tests")

    cfg = _alembic_config(test_db_url)

    # Drop + recreate the test schema to guarantee a clean, migrated state.
    # This is safe: the safety guard has already confirmed the target is
    # geo_tracker_test and APP_ENV=test.
    engine = create_engine(test_db_url, poolclass=NullPool)
    with engine.connect() as conn:
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
    return test_db_url


@pytest.fixture()
def db_session(prepared_test_db: str) -> Iterator[Session]:
    """Yield a session backed by a single connection with a rollback.

    Each test runs inside a transaction that is rolled back at the end,
    so tests are isolated and never pollute the test database.
    """
    from app.db.session import reset_engine

    reset_engine()
    engine: Engine = create_engine(prepared_test_db, poolclass=NullPool, future=True)

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
