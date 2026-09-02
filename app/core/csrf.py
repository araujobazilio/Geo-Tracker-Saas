"""CSRF protection middleware.

Validates CSRF tokens for state-changing requests (POST, PUT, PATCH, DELETE).

Strategy:
  - The session stores a CSRF token (generated at session creation).
  - The browser receives the CSRF token via GET /api/v1/auth/csrf.
  - State-changing requests must include the token in the `X-CSRF-Token` header.
  - The server validates the header token against the session's stored token
    using constant-time comparison.
  - GET/HEAD/OPTIONS requests do not require CSRF protection.

Exempt paths:
  - /api/v1/auth/login (no session yet)
  - /api/v1/auth/register (no session yet)
  - /health, /ready (infrastructure)
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings
from app.services.session_service import SessionService

# Methods that require CSRF protection.
_PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Paths exempt from CSRF (no session exists yet or infra endpoints).
_EXEMPT_PATHS = {
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/login",
    "/register",
    "/health",
    "/ready",
}


class CSRFMiddleware(BaseHTTPMiddleware):
    """Validate CSRF tokens on state-changing requests."""

    def __init__(self, app: object, session_service: SessionService | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._session_service = session_service

    def _get_session_service(self) -> SessionService:
        if self._session_service is None:
            settings = get_settings()
            from app.db.redis import get_redis

            self._session_service = SessionService(
                redis=get_redis(), ttl_seconds=settings.session_ttl_seconds
            )
        return self._session_service

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in _PROTECTED_METHODS:
            return await call_next(request)

        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        settings = get_settings()
        session_cookie = request.cookies.get(settings.session_cookie_name)

        # If there's no session cookie, there's nothing to CSRF-protect.
        # This allows idempotent logout for already-expired sessions.
        if not session_cookie:
            return await call_next(request)

        csrf_header = request.headers.get("X-CSRF-Token", "")

        if not csrf_header:
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "csrf_error", "message": "CSRF token required."}},
            )

        svc = self._get_session_service()
        if not svc.validate_csrf(session_cookie, csrf_header):
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "csrf_error", "message": "Invalid CSRF token."}},
            )

        return await call_next(request)
