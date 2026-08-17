"""Safety guard for destructive integration-test database operations.

Integration tests reset the test database schema (DROP + recreate) before
applying Alembic migrations. This is destructive, so it must be impossible
to accidentally target a non-test database (e.g. the development database
`geo_tracker` or a production database).

This module exposes a single validation function, `assert_test_database_safe`,
which enforces TWO independent conditions before any destructive operation:

1. The database name in the URL must be exactly `geo_tracker_test`.
2. `APP_ENV` must be exactly `test`.

Both conditions must hold (defense in depth). If either fails, a
`TestDatabaseSafetyError` is raised immediately. Error messages NEVER
include credentials (passwords) from the URL.
"""

from __future__ import annotations

import os

from sqlalchemy.engine import make_url

# The ONLY database name permitted for destructive test setup.
ALLOWED_TEST_DB_NAME = "geo_tracker_test"


class TestDatabaseSafetyError(RuntimeError):
    """Raised when a destructive test DB operation targets an unsafe database."""

    # Prevent pytest from collecting this class as a test (name starts with "Test").
    __test__ = False


def assert_test_database_safe(url: str, app_env: str | None = None) -> None:
    """Validate that `url` points to the dedicated test database and that
    the application environment is `test`.

    Raises:
        TestDatabaseSafetyError: if the database name is not
            `geo_tracker_test` or if `APP_ENV` is not `test`.
    """
    if app_env is None:
        app_env = os.environ.get("APP_ENV", "")

    # --- Condition 1: APP_ENV must be test ---
    if app_env != "test":
        raise TestDatabaseSafetyError(
            "Refusing destructive test database operation: "
            f"APP_ENV is {app_env!r}, expected 'test'."
        )

    # --- Condition 2: database name must be the dedicated test DB ---
    parsed = make_url(url)
    db_name = parsed.database

    if not db_name:
        raise TestDatabaseSafetyError(
            "Refusing destructive test database operation: " "target database name is empty."
        )

    if db_name != ALLOWED_TEST_DB_NAME:
        raise TestDatabaseSafetyError(
            "Refusing destructive test database operation: "
            f"target database is not {ALLOWED_TEST_DB_NAME} "
            f"(got {db_name!r})."
        )
