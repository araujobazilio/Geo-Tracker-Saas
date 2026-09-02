"""Security headers and request correlation middleware tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestSecurityHeaders:
    """Test that security headers are present on responses."""

    def test_security_headers_present(self) -> None:
        """Security headers are set on a normal response."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/health")
        assert response.status_code == 200
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert "Permissions-Policy" in response.headers
        assert "Content-Security-Policy" in response.headers

    def test_csp_contains_self(self) -> None:
        """CSP allows self for scripts and styles."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/health")
        csp = response.headers.get("Content-Security-Policy", "")
        assert "'self'" in csp

    def test_hsts_not_set_in_test(self) -> None:
        """HSTS is NOT set in test environment (non-HTTPS)."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/health")
        # In test env, HSTS should not be present.
        assert "Strict-Transport-Security" not in response.headers


class TestRequestCorrelation:
    """Test request correlation ID middleware."""

    def test_response_has_request_id(self) -> None:
        """Every response has an X-Request-ID header."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/health")
        assert response.status_code == 200
        request_id = response.headers.get("X-Request-ID")
        assert request_id is not None
        assert len(request_id) > 0

    def test_incoming_request_id_preserved(self) -> None:
        """A valid incoming X-Request-ID is preserved in the response."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        custom_id = "test-request-id-12345"
        response = client.get("/health", headers={"X-Request-ID": custom_id})
        assert response.status_code == 200
        assert response.headers.get("X-Request-ID") == custom_id

    def test_invalid_request_id_replaced(self) -> None:
        """An invalid incoming X-Request-ID is replaced with a generated one."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        # Contains invalid characters (spaces, special chars).
        invalid_id = "invalid id with spaces!"
        response = client.get("/health", headers={"X-Request-ID": invalid_id})
        assert response.status_code == 200
        request_id = response.headers.get("X-Request-ID")
        assert request_id is not None
        assert request_id != invalid_id

    def test_generated_request_id_is_hex(self) -> None:
        """A generated request ID is a valid UUID4 hex string."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/health")
        request_id = response.headers.get("X-Request-ID", "")
        # UUID4 hex is 32 chars, alphanumeric.
        assert len(request_id) == 32
        assert all(c in "0123456789abcdef" for c in request_id)


class TestTrustedHost:
    """Test trusted host validation middleware."""

    def test_all_hosts_allowed_in_test(self) -> None:
        """In test environment (empty ALLOWED_HOSTS), all hosts are allowed."""
        from app.main import create_app

        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/health", headers={"host": "evil.example"})
        # In test, ALLOWED_HOSTS is empty, so all hosts are allowed.
        assert response.status_code == 200
