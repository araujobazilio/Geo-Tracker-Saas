"""Production security static tests.

Validates production deployment artifacts without running them:
- Dockerfile runtime USER is non-root
- docker-compose.prod.yml has no public DB/Redis ports, no --reload
- Production assets contain no real secrets
- .env.production.example is tracked and has no real secrets
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestDockerfileSecurity:
    """Test the production Dockerfile for security requirements."""

    @pytest.fixture()
    def dockerfile_content(self) -> str:
        return (_REPO_ROOT / "Dockerfile").read_text()

    def test_dockerfile_exists(self) -> None:
        assert (_REPO_ROOT / "Dockerfile").exists()

    def test_runtime_user_non_root(self, dockerfile_content: str) -> None:
        """The Dockerfile must specify a non-root runtime USER."""
        assert "USER" in dockerfile_content
        # Find the last USER directive (the runtime stage).
        user_lines = [
            line.strip()
            for line in dockerfile_content.splitlines()
            if line.strip().startswith("USER")
        ]
        assert len(user_lines) > 0
        last_user = user_lines[-1].upper()
        assert "root" not in last_user.lower(), f"Runtime USER is root: {last_user}"

    def test_no_reload_in_production(self, dockerfile_content: str) -> None:
        """The Dockerfile CMD/ENTRYPOINT must not use --reload (production)."""
        # Check only non-comment lines for --reload.
        for line in dockerfile_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "--reload" not in stripped, f"Found --reload in Dockerfile: {line}"

    def test_multi_stage_build(self, dockerfile_content: str) -> None:
        """The Dockerfile should use multi-stage builds."""
        from_as_count = sum(
            1
            for line in dockerfile_content.splitlines()
            if line.strip().startswith("FROM ") and " AS " in line.upper()
        )
        assert from_as_count >= 2, "Dockerfile should have at least 2 stages (AS)"

    def test_no_dev_deps_in_runtime(self, dockerfile_content: str) -> None:
        """The runtime stage should not install dev/test dependencies."""
        # Check that pytest/ruff/mypy are not pip-installed in the runtime stage.
        # They might appear in a builder stage, but not in the final stage.
        lines = dockerfile_content.splitlines()
        # Find the last FROM line (runtime stage).
        last_from_idx = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("FROM "):
                last_from_idx = i
        runtime_stage = "\n".join(lines[last_from_idx:])
        assert "pytest" not in runtime_stage.lower()
        assert "ruff" not in runtime_stage.lower()
        assert "mypy" not in runtime_stage.lower()
        assert "ipython" not in runtime_stage.lower()


class TestProductionComposeSecurity:
    """Test docker-compose.prod.yml for security requirements."""

    @pytest.fixture()
    def compose_data(self) -> dict:
        content = (_REPO_ROOT / "docker-compose.prod.yml").read_text()
        return yaml.safe_load(content)

    def test_compose_exists(self) -> None:
        assert (_REPO_ROOT / "docker-compose.prod.yml").exists()

    def test_no_public_postgres_port(self, compose_data: dict) -> None:
        """PostgreSQL must NOT publish ports to the host."""
        postgres = compose_data.get("services", {}).get("postgres", {})
        ports = postgres.get("ports", [])
        assert not ports, f"PostgreSQL must not publish ports: {ports}"

    def test_no_public_redis_port(self, compose_data: dict) -> None:
        """Redis must NOT publish ports to the host."""
        redis = compose_data.get("services", {}).get("redis", {})
        ports = redis.get("ports", [])
        assert not ports, f"Redis must not publish ports: {ports}"

    def test_app_no_public_port(self, compose_data: dict) -> None:
        """App must NOT publish port 8000 to the host (Nginx proxies)."""
        app = compose_data.get("services", {}).get("app", {})
        ports = app.get("ports", [])
        assert not ports, f"App must not publish ports: {ports}"

    def test_no_reload_in_app(self, compose_data: dict) -> None:
        """App command must not use --reload."""
        app = compose_data.get("services", {}).get("app", {})
        command = str(app.get("command", ""))
        assert "--reload" not in command

    def test_nginx_publishes_http_https(self, compose_data: dict) -> None:
        """Nginx should publish 80 and 443."""
        nginx = compose_data.get("services", {}).get("nginx", {})
        ports = nginx.get("ports", [])
        port_strs = [str(p) for p in ports]
        assert any("80" in p for p in port_strs), f"Nginx should publish 80: {ports}"
        assert any("443" in p for p in port_strs), f"Nginx should publish 443: {ports}"

    def test_migrate_is_separate(self, compose_data: dict) -> None:
        """Migration should be a separate one-shot service with profiles."""
        migrate = compose_data.get("services", {}).get("migrate", {})
        assert migrate, "migrate service must exist"
        profiles = migrate.get("profiles", [])
        assert "migrate" in profiles, "migrate service must have 'migrate' profile"

    def test_beat_singleton(self, compose_data: dict) -> None:
        """Beat should have deploy.replicas=1 (singleton)."""
        beat = compose_data.get("services", {}).get("beat", {})
        deploy = beat.get("deploy", {})
        replicas = deploy.get("replicas", 1)
        assert replicas == 1, f"Beat must be a singleton (replicas=1): {replicas}"


class TestEnvProductionExample:
    """Test .env.production.example is tracked and safe."""

    def test_file_exists(self) -> None:
        assert (_REPO_ROOT / ".env.production.example").exists()

    def test_no_real_secrets(self) -> None:
        """The example file must not contain real secrets."""
        content = (_REPO_ROOT / ".env.production.example").read_text()
        # Must not contain the dev password.
        assert "geo_tracker_dev_password" not in content
        # Must not contain real API key patterns (sk-...).
        assert "sk-" not in content
        # APP_SECRET_KEY must be a placeholder.
        for line in content.splitlines():
            if line.startswith("APP_SECRET_KEY="):
                val = line.split("=", 1)[1]
                assert "<" in val or val == "", f"APP_SECRET_KEY must be a placeholder: {val}"

    def test_no_private_keys(self) -> None:
        """The example file must not contain private keys."""
        content = (_REPO_ROOT / ".env.production.example").read_text()
        assert "BEGIN PRIVATE KEY" not in content
        assert "BEGIN RSA PRIVATE KEY" not in content
        assert "BEGIN EC PRIVATE KEY" not in content


class TestProductionAssetsNoSecrets:
    """Test that production assets don't contain real secrets."""

    def test_dockerfile_no_dev_password(self) -> None:
        content = (_REPO_ROOT / "Dockerfile").read_text()
        assert "geo_tracker_dev_password" not in content

    def test_compose_no_dev_password(self) -> None:
        content = (_REPO_ROOT / "docker-compose.prod.yml").read_text()
        assert "geo_tracker_dev_password" not in content

    def test_nginx_config_no_secrets(self) -> None:
        nginx_conf = _REPO_ROOT / "docker" / "nginx" / "nginx.conf"
        if nginx_conf.exists():
            content = nginx_conf.read_text()
            assert "geo_tracker_dev_password" not in content
            assert "BEGIN PRIVATE KEY" not in content
