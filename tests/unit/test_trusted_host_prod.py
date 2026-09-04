"""Production trusted host and docs exposure tests.

Tests that:
- ALLOWED_HOSTS is enforced (approved host accepted, spoofed host rejected).
- /docs, /redoc, /openapi.json are NOT exposed in production.
- The original healthcheck Host bug is reproduced and fixed.
- /health and /ready do not globally bypass host validation.
- Empty/malformed Host does not become accepted in production.
- Customer routes remain protected.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _prod_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure production-like settings for trusted host tests."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:strongpass@db:5432/geo")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("APP_PUBLIC_BASE_URL", "https://geo.example.com")
    monkeypatch.setenv("ALLOWED_HOSTS", "geo.example.com")
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    monkeypatch.setenv("DEV_SEED_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestTrustedHostProduction:
    """Test trusted host validation in production mode."""

    def test_approved_host_accepted(self, _prod_settings) -> None:
        """A request with an approved Host header is accepted."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/health", headers={"host": "geo.example.com"})
        assert response.status_code == 200

    def test_spoofed_host_rejected(self, _prod_settings) -> None:
        """A request with a spoofed Host header is rejected with 400."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/health", headers={"host": "evil.example.com"})
        assert response.status_code == 400

    def test_approved_host_with_port_accepted(self, _prod_settings) -> None:
        """A request with an approved host + port is accepted."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        # TestClient uses base_url, so we need to check that the trusted host
        # middleware accepts host:port format. The middleware should strip
        # the port for comparison.
        response = client.get("/health", headers={"host": "geo.example.com:443"})
        assert response.status_code == 200

    def test_localhost_host_rejected_in_production(self, _prod_settings) -> None:
        """Regression: Host=localhost must be rejected in production.

        This is the original healthcheck bug — the Dockerfile/compose
        healthcheck used to send Host: localhost which TrustedHostMiddleware
        correctly rejects when ALLOWED_HOSTS does not include localhost.
        """
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/health", headers={"host": "localhost"})
        assert response.status_code == 400

    def test_localhost_host_with_port_rejected_in_production(self, _prod_settings) -> None:
        """Regression: Host=localhost:8000 must also be rejected."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/health", headers={"host": "localhost:8000"})
        assert response.status_code == 400

    def test_empty_host_rejected_in_production(self, _prod_settings) -> None:
        """An empty Host header must not be accepted in production."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/health", headers={"host": ""})
        assert response.status_code == 400

    def test_health_does_not_bypass_host_validation(self, _prod_settings) -> None:
        """/health must not globally bypass TrustedHostMiddleware."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        # Spoofed host on /health must still be rejected.
        response = client.get("/health", headers={"host": "malicious.example.test"})
        assert response.status_code == 400

    def test_ready_does_not_bypass_host_validation(self, _prod_settings) -> None:
        """/ready must not globally bypass TrustedHostMiddleware."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        # Spoofed host on /ready must still be rejected.
        response = client.get("/ready", headers={"host": "malicious.example.test"})
        assert response.status_code == 400

    def test_customer_route_protected(self, _prod_settings) -> None:
        """A normal customer route with an unauthorized Host must get 400."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/", headers={"host": "malicious.example.test"})
        assert response.status_code == 400

    def test_authorized_host_customer_route_accepted(self, _prod_settings) -> None:
        """A normal customer route with an authorized Host must not get 400."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/", headers={"host": "geo.example.com"})
        # We don't care if it's 200 or a redirect — just not 400 (host rejected).
        assert response.status_code != 400


class TestDocsNotExposedInProduction:
    """Test that /docs, /redoc, /openapi.json are not exposed in production."""

    def test_docs_not_exposed(self, _prod_settings) -> None:
        """/docs returns 404 in production."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/docs", headers={"host": "geo.example.com"})
        assert response.status_code == 404

    def test_redoc_not_exposed(self, _prod_settings) -> None:
        """/redoc returns 404 in production."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/redoc", headers={"host": "geo.example.com"})
        assert response.status_code == 404

    def test_openapi_json_not_exposed(self, _prod_settings) -> None:
        """/openapi.json returns 404 in production."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/openapi.json", headers={"host": "geo.example.com"})
        assert response.status_code == 404
