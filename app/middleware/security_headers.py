"""Security headers middleware.

Adds safe HTTP security headers to all responses:
- Strict-Transport-Security (only in staging/production)
- X-Content-Type-Options: nosniff
- Referrer-Policy: strict-origin-when-cross-origin
- X-Frame-Options: DENY
- Permissions-Policy (restrict camera, microphone, geolocation)
- Content-Security-Policy (allows self + inline scripts/styles for HTMX/Jinja)

The CSP is deliberately permissive enough for the existing Jinja/HTMX/Chart.js
application while blocking external origins. Inline scripts are allowed because
the current application uses small inline <script> blocks for HTMX config and
Chart.js initialization. A future refactoring could move these to static JS
files and tighten the CSP further.

In development/test, HSTS is NOT sent to avoid breaking local HTTP testing.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.config import get_settings

# CSP that allows:
# - scripts from self and vendored static (htmx, chart.js)
# - inline scripts (for HTMX config + Chart.js init)
# - styles from self and inline
# - images from self and data:
# - connect to self (HTMX/fetch)
# - frame-ancestors none (clickjacking protection)
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=()"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add production-safe HTTP security headers to all responses."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        # Always set these headers.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = _PERMISSIONS_POLICY
        response.headers["Content-Security-Policy"] = _CSP

        # HSTS only in staging/production (HTTPS environments).
        settings = get_settings()
        if settings.is_staging or settings.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        return response
