"""Trusted host validation middleware.

In staging/production, rejects requests with arbitrary Host headers that
are not in the ALLOWED_HOSTS configuration. This prevents Host header
spoofing attacks.

In development/test, ALLOWED_HOSTS is typically empty, meaning all hosts
are allowed (preserving localhost ergonomics).

The middleware checks the Host header (without port) against the
allowed_host_list. If the list is empty, all hosts are allowed.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings


class TrustedHostMiddleware(BaseHTTPMiddleware):
    """Reject requests with disallowed Host headers in production."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        allowed = settings.allowed_host_list

        if allowed:
            host = request.headers.get("host", "").split(":")[0].lower()
            if host not in [h.lower() for h in allowed]:
                return JSONResponse(
                    status_code=400,
                    content={"error": {"code": "bad_request", "message": "Invalid Host header."}},
                )

        return await call_next(request)
