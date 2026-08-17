"""Integration test configuration.

Integration tests run against a REAL PostgreSQL instance (the local dev
database via Docker Compose). They are marked with `@pytest.mark.integration`
and skipped unless the database is reachable.

Tests use a per-test transaction that is rolled back, so they are isolated
and never leave persistent state.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

# Force test environment before any app imports.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_JSON", "false")

# Integration tests need a real database. Default to the Docker Compose
# dev database on the remapped host port.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker",
)

from app.db.base import Base
from app.db.session import reset_engine
from app.models import *  # noqa: F403  # register models on metadata


def _database_available() -> bool:
    url = os.environ["DATABASE_URL"]
    try:
        engine = create_engine(url, poolclass=NullPool)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


DB_AVAILABLE = _database_available()


@pytest.fixture()
def db_session() -> Session:
    """Yield a session backed by a single connection with a rollback.

    Each test runs inside a transaction that is rolled back at the end,
    so tests are isolated and never pollute the shared dev database.
    """
    if not DB_AVAILABLE:
        pytest.skip("PostgreSQL not available for integration tests")

    reset_engine()
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url, poolclass=NullPool, future=True)

    # Ensure schema exists (create_all is idempotent; Alembic is the
    # canonical migration path, but tests may run against a fresh DB).
    Base.metadata.create_all(engine)

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
