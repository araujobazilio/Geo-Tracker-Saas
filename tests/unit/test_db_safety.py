"""Unit tests for the test-database safety guard.

Verifies that destructive test setup is only allowed when:
  - the database name is exactly `geo_tracker_test`, AND
  - APP_ENV is exactly `test`.

All other combinations must be rejected. No test here performs any
real database operation — only the pure validation function is tested.
"""

from __future__ import annotations

import pytest

from tests.integration.db_safety import TestDatabaseSafetyError, assert_test_database_safe

_BASE_URL = "postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432"


def _url(db: str) -> str:
    return f"{_BASE_URL}/{db}"


# --- ACCEPT cases ---


def test_accepts_geo_tracker_test_with_test_env() -> None:
    assert_test_database_safe(_url("geo_tracker_test"), app_env="test")


def test_accepts_geo_tracker_test_default_env_from_os(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    assert_test_database_safe(_url("geo_tracker_test"))


# --- REJECT: wrong database name ---


def test_rejects_geo_tracker_dev_db() -> None:
    with pytest.raises(TestDatabaseSafetyError, match="not geo_tracker_test"):
        assert_test_database_safe(_url("geo_tracker"), app_env="test")


def test_rejects_postgres_db() -> None:
    with pytest.raises(TestDatabaseSafetyError, match="not geo_tracker_test"):
        assert_test_database_safe(_url("postgres"), app_env="test")


def test_rejects_empty_database_name() -> None:
    with pytest.raises(TestDatabaseSafetyError, match="empty"):
        assert_test_database_safe(_url(""), app_env="test")


def test_rejects_arbitrary_database_name() -> None:
    with pytest.raises(TestDatabaseSafetyError, match="not geo_tracker_test"):
        assert_test_database_safe(_url("production_db"), app_env="test")


# --- REJECT: wrong APP_ENV ---


def test_rejects_development_env() -> None:
    with pytest.raises(TestDatabaseSafetyError, match="APP_ENV"):
        assert_test_database_safe(_url("geo_tracker_test"), app_env="development")


def test_rejects_staging_env() -> None:
    with pytest.raises(TestDatabaseSafetyError, match="APP_ENV"):
        assert_test_database_safe(_url("geo_tracker_test"), app_env="staging")


def test_rejects_production_env() -> None:
    with pytest.raises(TestDatabaseSafetyError, match="APP_ENV"):
        assert_test_database_safe(_url("geo_tracker_test"), app_env="production")


def test_rejects_missing_env() -> None:
    with pytest.raises(TestDatabaseSafetyError, match="APP_ENV"):
        assert_test_database_safe(_url("geo_tracker_test"), app_env="")


# --- REJECT: both wrong ---


def test_rejects_wrong_db_and_wrong_env() -> None:
    with pytest.raises(TestDatabaseSafetyError):
        assert_test_database_safe(_url("geo_tracker"), app_env="production")


# --- Error message must not leak credentials ---


def test_error_does_not_leak_password() -> None:
    secret_url = "postgresql+psycopg://user:super-secret-pw@host:5432/geo_tracker"
    with pytest.raises(TestDatabaseSafetyError) as exc:
        assert_test_database_safe(secret_url, app_env="test")
    assert "super-secret-pw" not in str(exc.value)
