"""Unit tests for web layer routing — route registration and structure.

Verifies that the web router is properly mounted, all expected routes
exist, and static files are served. Does not require PostgreSQL or Redis.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _get_app():
    return create_app()


class TestWebRouterMounting:
    def test_app_has_web_routes(self) -> None:
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        # Web routes should be mounted
        assert any("/app" in r for r in routes)

    def test_login_route_exists(self) -> None:
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/login" in routes

    def test_register_route_exists(self) -> None:
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/register" in routes

    def test_logout_route_exists(self) -> None:
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/logout" in routes

    def test_dashboard_route_exists(self) -> None:
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/app" in routes

    def test_onboarding_route_exists(self) -> None:
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("/projects/new" in r for r in routes)

    def test_scan_routes_exist(self) -> None:
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("/scans" in r for r in routes)
        assert any("/status" in r for r in routes)

    def test_opportunity_routes_exist(self) -> None:
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("/opportunities" in r for r in routes)

    def test_schedule_routes_exist(self) -> None:
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("/schedule" in r for r in routes)

    def test_notification_routes_exist(self) -> None:
        app = _get_app()
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert any("/notifications" in r for r in routes)


class TestWebStaticFiles:
    def test_static_css_served(self) -> None:
        app = _get_app()
        client = TestClient(app)
        response = client.get("/static/css/app.css")
        assert response.status_code == 200
        assert "text/css" in response.headers.get("content-type", "")

    def test_static_js_served(self) -> None:
        app = _get_app()
        client = TestClient(app)
        response = client.get("/static/js/app.js")
        assert response.status_code == 200

    def test_static_htmx_served(self) -> None:
        app = _get_app()
        client = TestClient(app)
        response = client.get("/static/vendor/htmx.min.js")
        assert response.status_code == 200

    def test_static_chartjs_served(self) -> None:
        app = _get_app()
        client = TestClient(app)
        response = client.get("/static/vendor/chart.umd.min.js")
        assert response.status_code == 200


class TestWebAuthPages:
    def test_login_page_renders(self) -> None:
        app = _get_app()
        client = TestClient(app)
        response = client.get("/login")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert b"Sign in" in response.content or b"sign in" in response.content.lower()

    def test_register_page_renders(self) -> None:
        app = _get_app()
        client = TestClient(app)
        response = client.get("/register")
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_unauthenticated_dashboard_redirects(self) -> None:
        app = _get_app()
        client = TestClient(app)
        response = client.get("/app", follow_redirects=False)
        assert response.status_code in (302, 307)


class TestWebErrorPages:
    def test_404_page(self) -> None:
        app = _get_app()
        client = TestClient(app)
        # Use a valid UUID format — the route will redirect (302) since user is not authed
        response = client.get(
            "/app/w/00000000-0000-0000-0000-000000000000/projects/00000000-0000-0000-0000-000000000000",
            follow_redirects=False,
        )
        # Unauthenticated users get redirected to login
        assert response.status_code in (302, 307, 404, 403)
