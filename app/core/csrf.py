"""CSRF protection middleware.

Validates CSRF tokens for state-changing requests (POST, PUT, PATCH, DELETE).

Strategy:
  - The session stores a CSRF token (generated at session creation).
  - The browser receives the CSRF token via GET /api/v1/auth/csrf or
    the ``csrf_token`` template variable (rendered in a hidden form field
    and in a ``<meta>`` tag for HTMX).
  - State-changing requests must include the token EITHER:
    - in the ``X-CSRF-Token`` header (HTMX, fetch, API clients), OR
    - in the ``csrf_token`` field of an ``application/x-www-form-urlencoded``
      or ``multipart/form-data`` body (native browser form submission).
  - The server validates the presented token against the session's stored
    token using constant-time comparison.
  - GET/HEAD/OPTIONS requests do not require CSRF protection.

Exempt paths:
  - /api/v1/auth/login (no session yet)
  - /api/v1/auth/register (no session yet)
  - /login, /register (web auth — no session yet)
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

# Content types that may contain a csrf_token form field.
_FORM_CONTENT_TYPES = {"application/x-www-form-urlencoded", "multipart/form-data"}


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

    async def _extract_csrf_token(self, request: Request) -> str:
        """Extract CSRF token from header or form body.

        For form bodies, we read the body and cache it so downstream
        handlers can still access it via request.form() or request.body().
        """
        # 1. Check header first (HTMX, fetch, API clients).
        header_token = request.headers.get("X-CSRF-Token", "")
        if header_token:
            return header_token

        # 2. Check form body for native browser submissions.
        content_type = request.headers.get("content-type", "").split(";")[0].strip()
        if content_type not in _FORM_CONTENT_TYPES:
            return ""

        # Read and cache the body so it can be replayed for the actual handler.
        body = await request.body()
        if not body:
            return ""

        # Parse the form data from the cached body.
        try:
            from urllib.parse import parse_qs

            if content_type == "multipart/form-data":
                # For multipart, use Starlette's form parser with the cached body.
                # We need to set _body so request.form() can reuse it.
                request._body = body
                form = await request.form()
                token = form.get("csrf_token", "")
                return str(token) if token else ""
            else:
                # For urlencoded, parse directly.
                parsed = parse_qs(body.decode("utf-8", errors="replace"))
                values = parsed.get("csrf_token", [])
                return values[0] if values else ""
        except Exception:
            return ""

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

        csrf_token = await self._extract_csrf_token(request)

        if not csrf_token:
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "csrf_error", "message": "CSRF token required."}},
            )

        svc = self._get_session_service()
        if not svc.validate_csrf(session_cookie, csrf_token):
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "csrf_error", "message": "Invalid CSRF token."}},
            )

        return await call_next(request)
