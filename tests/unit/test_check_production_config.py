"""Tests for scripts/check_production_config.py project root calculation."""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestProjectRootCalculation:
    """Test that PROJECT_ROOT in check_production_config points to the repo root."""

    def test_project_root_is_repo_root(self) -> None:
        """PROJECT_ROOT should be the repository root."""
        from scripts.check_production_config import PROJECT_ROOT

        # PROJECT_ROOT should resolve to the same as the repo root.
        assert _REPO_ROOT.resolve() == PROJECT_ROOT

    def test_alembic_ini_exists_at_project_root(self) -> None:
        """alembic.ini should exist at PROJECT_ROOT."""
        from scripts.check_production_config import PROJECT_ROOT

        assert (PROJECT_ROOT / "alembic.ini").exists()

    def test_alembic_dir_exists_at_project_root(self) -> None:
        """alembic/ directory should exist at PROJECT_ROOT."""
        from scripts.check_production_config import PROJECT_ROOT

        assert (PROJECT_ROOT / "alembic").is_dir()

    def test_project_root_not_too_far_up(self) -> None:
        """PROJECT_ROOT should not walk too far up (e.g. above repo root)."""
        from scripts.check_production_config import PROJECT_ROOT

        # The repo root should contain pyproject.toml.
        assert (PROJECT_ROOT / "pyproject.toml").exists()
        # It should also contain the app/ directory.
        assert (PROJECT_ROOT / "app").is_dir()
        # It should also contain the scripts/ directory.
        assert (PROJECT_ROOT / "scripts").is_dir()
