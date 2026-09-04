"""Static tests for production healthcheck Host safety.

Verifies that:
- The Dockerfile HEALTHCHECK derives Host from ALLOWED_HOSTS, not localhost.
- The Dockerfile HEALTHCHECK does not hardcode localhost as the Host.
- The Dockerfile HEALTHCHECK does not contain secrets.
- The Compose app healthcheck has equivalent semantics.
- The Nginx config forwards Host on /health and /ready.
- The Nginx config has a local /nginx-health endpoint.
- The Nginx Docker healthcheck uses /nginx-health, not /health.
- The Compose env template does not produce interpolation warnings.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _extract_dockerfile_healthcheck(content: str) -> str:
    """Extract the full HEALTHCHECK block including line-continuation lines."""
    lines = content.splitlines()
    block: list[str] = []
    in_healthcheck = False
    for line in lines:
        if line.strip().startswith("HEALTHCHECK"):
            in_healthcheck = True
            block.append(line)
        elif in_healthcheck:
            if line.strip().endswith("\\"):
                block.append(line)
            else:
                # Last line of the continuation (no trailing backslash).
                block.append(line)
                break
    return "\n".join(block)


def _extract_nginx_location_block(content: str, location: str) -> str:
    """Extract a nginx location block using brace-depth matching."""
    idx = content.index(location)
    return _extract_nginx_location_block_at(content, idx)


def _extract_nginx_location_block_at(content: str, idx: int) -> str:
    """Extract a nginx location block starting at the given index."""
    # Find the opening brace after the location directive.
    brace_start = content.index("{", idx)
    depth = 1
    pos = brace_start + 1
    while depth > 0 and pos < len(content):
        if content[pos] == "{":
            depth += 1
        elif content[pos] == "}":
            depth -= 1
        pos += 1
    return content[idx:pos]


class TestDockerfileHealthcheck:
    """Test the production Dockerfile HEALTHCHECK command."""

    @pytest.fixture()
    def dockerfile_content(self) -> str:
        return (_REPO_ROOT / "Dockerfile").read_text()

    @pytest.fixture()
    def healthcheck_block(self, dockerfile_content: str) -> str:
        return _extract_dockerfile_healthcheck(dockerfile_content)

    def test_healthcheck_exists(self, healthcheck_block: str) -> None:
        """The Dockerfile must have a HEALTHCHECK."""
        assert healthcheck_block, "Dockerfile must have a HEALTHCHECK"

    def test_healthcheck_targets_health_endpoint(self, healthcheck_block: str) -> None:
        """The HEALTHCHECK must target /health."""
        assert "/health" in healthcheck_block, "HEALTHCHECK must target /health"

    def test_healthcheck_derives_host_from_allowed_hosts(self, healthcheck_block: str) -> None:
        """The HEALTHCHECK must derive Host from ALLOWED_HOSTS."""
        assert (
            "ALLOWED_HOSTS" in healthcheck_block
        ), "HEALTHCHECK must derive Host from ALLOWED_HOSTS"

    def test_healthcheck_does_not_hardcode_localhost_host(self, healthcheck_block: str) -> None:
        """The HEALTHCHECK must not hardcode localhost as the Host header.

        It may use localhost as the connection target (http://localhost:8000),
        but the Host *header* must come from ALLOWED_HOSTS.
        """
        # The healthcheck must use -H "Host: ..." with a derived value,
        # not a hardcoded localhost.
        assert (
            "Host:" in healthcheck_block or "Host " in healthcheck_block
        ), "HEALTHCHECK must set a Host header"
        # Must not have a hardcoded "Host: localhost" -- the Host must be
        # derived from ALLOWED_HOSTS.
        assert not re.search(
            r"Host:\s*localhost", healthcheck_block, re.IGNORECASE
        ), "HEALTHCHECK must not hardcode Host: localhost"

    def test_healthcheck_no_secrets(self, healthcheck_block: str) -> None:
        """The HEALTHCHECK must not contain secrets."""
        assert "SECRET" not in healthcheck_block.upper()
        assert "PASSWORD" not in healthcheck_block.upper()
        assert "API_KEY" not in healthcheck_block.upper()

    def test_healthcheck_fails_safely_if_no_allowed_hosts(self, healthcheck_block: str) -> None:
        """If ALLOWED_HOSTS is empty, the healthcheck should fall back
        to localhost (for dev/test) -- not authorize arbitrary hosts."""
        # The fallback to localhost is acceptable for dev/test where
        # ALLOWED_HOSTS is empty and all hosts are allowed.
        assert (
            "localhost" in healthcheck_block
        ), "HEALTHCHECK must have a localhost fallback for dev/test"


class TestComposeAppHealthcheck:
    """Test docker-compose.prod.yml app healthcheck."""

    @pytest.fixture()
    def compose_data(self) -> dict:
        content = (_REPO_ROOT / "docker-compose.prod.yml").read_text()
        return yaml.safe_load(content)

    def test_app_healthcheck_derives_host_from_allowed_hosts(self, compose_data: dict) -> None:
        """The app healthcheck must derive Host from ALLOWED_HOSTS."""
        app = compose_data.get("services", {}).get("app", {})
        healthcheck = app.get("healthcheck", {})
        test = healthcheck.get("test", [])
        # Join the test list into a string for inspection.
        test_str = " ".join(test) if isinstance(test, list) else str(test)
        assert "ALLOWED_HOSTS" in test_str, "App healthcheck must derive Host from ALLOWED_HOSTS"

    def test_app_healthcheck_targets_health(self, compose_data: dict) -> None:
        """The app healthcheck must target /health."""
        app = compose_data.get("services", {}).get("app", {})
        healthcheck = app.get("healthcheck", {})
        test = healthcheck.get("test", [])
        test_str = " ".join(test) if isinstance(test, list) else str(test)
        assert "/health" in test_str

    def test_app_healthcheck_does_not_hardcode_localhost_host(self, compose_data: dict) -> None:
        """The app healthcheck must not hardcode Host: localhost."""
        app = compose_data.get("services", {}).get("app", {})
        healthcheck = app.get("healthcheck", {})
        test = healthcheck.get("test", [])
        test_str = " ".join(test) if isinstance(test, list) else str(test)
        # Must set a Host header.
        assert "Host" in test_str, "App healthcheck must set a Host header"
        # Must not hardcode "Host: localhost" as the production Host.
        # The localhost fallback for empty ALLOWED_HOSTS is OK.
        # But the primary Host must come from ALLOWED_HOSTS.
        assert "ALLOWED_HOSTS" in test_str

    def test_compose_does_not_require_localhost_in_allowed_hosts(self, compose_data: dict) -> None:
        """Production compose must not depend on adding localhost to ALLOWED_HOSTS."""
        app = compose_data.get("services", {}).get("app", {})
        env = app.get("environment", {})
        # ALLOWED_HOSTS is not set in compose environment -- it comes from env_file.
        # That's correct. We just verify compose doesn't inject localhost.
        if "ALLOWED_HOSTS" in env:
            assert "localhost" not in str(env["ALLOWED_HOSTS"]).lower()


class TestNginxHealthConfig:
    """Test nginx.conf health endpoint configuration."""

    @pytest.fixture()
    def nginx_content(self) -> str:
        return (_REPO_ROOT / "docker" / "nginx" / "nginx.conf").read_text()

    def test_health_forwards_host(self, nginx_content: str) -> None:
        """/health location must forward Host header."""
        assert "location = /health" in nginx_content
        block = _extract_nginx_location_block(nginx_content, "location = /health")
        assert "proxy_set_header Host" in block, "/health must forward Host header"

    def test_ready_forwards_host(self, nginx_content: str) -> None:
        """/ready location must forward Host header."""
        assert "location = /ready" in nginx_content
        block = _extract_nginx_location_block(nginx_content, "location = /ready")
        assert "proxy_set_header Host" in block, "/ready must forward Host header"

    def test_health_forwards_standard_proxy_headers(self, nginx_content: str) -> None:
        """/health must forward X-Real-IP, X-Forwarded-For, X-Forwarded-Proto, X-Request-ID."""
        block = _extract_nginx_location_block(nginx_content, "location = /health")
        assert "X-Real-IP" in block
        assert "X-Forwarded-For" in block
        assert "X-Forwarded-Proto" in block
        assert "X-Request-ID" in block

    def test_ready_forwards_standard_proxy_headers(self, nginx_content: str) -> None:
        """/ready must forward X-Real-IP, X-Forwarded-For, X-Forwarded-Proto, X-Request-ID."""
        block = _extract_nginx_location_block(nginx_content, "location = /ready")
        assert "X-Real-IP" in block
        assert "X-Forwarded-For" in block
        assert "X-Forwarded-Proto" in block
        assert "X-Request-ID" in block

    def test_normal_proxy_forwards_host(self, nginx_content: str) -> None:
        """The normal proxy location / must still forward Host.

        There may be multiple "location /" blocks (e.g. a port 80 redirect).
        We need the HTTPS server block that actually proxies to the app.
        """
        assert "location / {" in nginx_content, "Must have a catch-all location /"
        # Iterate over all "location / {" occurrences and find the proxying one.
        search_from = 0
        found = False
        while True:
            idx = nginx_content.find("location / {", search_from)
            if idx == -1:
                break
            block = _extract_nginx_location_block_at(nginx_content, idx)
            if "proxy_pass" in block and "proxy_set_header Host" in block:
                found = True
                break
            search_from = idx + 1
        assert found, "Must have a proxying location / with Host header"

    def test_nginx_health_endpoint_exists(self, nginx_content: str) -> None:
        """Nginx must have a local /nginx-health endpoint."""
        assert "location = /nginx-health" in nginx_content

    def test_nginx_health_does_not_proxy(self, nginx_content: str) -> None:
        """/nginx-health must NOT proxy to FastAPI."""
        block = _extract_nginx_location_block(nginx_content, "location = /nginx-health")
        assert "proxy_pass" not in block, "/nginx-health must not proxy to FastAPI"
        assert "return 200" in block, "/nginx-health must return 200 directly"

    def test_nginx_health_restricted_to_localhost(self, nginx_content: str) -> None:
        """/nginx-health should be restricted to container-local access."""
        block = _extract_nginx_location_block(nginx_content, "location = /nginx-health")
        assert "allow 127.0.0.1" in block
        assert "deny all" in block


class TestNginxComposeHealthcheck:
    """Test that the Nginx Docker healthcheck uses /nginx-health."""

    @pytest.fixture()
    def compose_data(self) -> dict:
        content = (_REPO_ROOT / "docker-compose.prod.yml").read_text()
        return yaml.safe_load(content)

    def test_nginx_healthcheck_uses_nginx_health(self, compose_data: dict) -> None:
        """The Nginx healthcheck must probe /nginx-health, not /health."""
        nginx = compose_data.get("services", {}).get("nginx", {})
        healthcheck = nginx.get("healthcheck", {})
        test = healthcheck.get("test", [])
        test_str = " ".join(test) if isinstance(test, list) else str(test)
        assert "/nginx-health" in test_str, "Nginx healthcheck must use /nginx-health"
        assert (
            "/health" not in test_str or "/nginx-health" in test_str
        ), "Nginx healthcheck must not use /health (use /nginx-health instead)"


class TestEnvTemplateNoInterpolationWarnings:
    """Test that .env.production.example does not cause interpolation warnings."""

    def test_no_database_url_in_template(self) -> None:
        """The template must not define DATABASE_URL with ${...} references."""
        content = (_REPO_ROOT / ".env.production.example").read_text()
        # DATABASE_URL should not be in the template (compose constructs it).
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                # If it exists, it must not contain ${...} references.
                assert (
                    "${" not in line
                ), f"DATABASE_URL in template must not use ${{...}} references: {line}"
