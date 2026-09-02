"""Structured request logging middleware.

For every HTTP request, logs:
- request_id (from correlation middleware)
- method
- path (without query parameters to avoid leaking sensitive data)
- status_code
- duration_ms

Does NOT log:
- cookies
- authorization headers
- CSRF tokens
- request bodies
- query parameters
- provider secrets

This middleware complements RequestCorrelationMiddleware by adding a
structured log entry at the end of each request.
"""

from __future__ import annotations

import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_log = structlog.get_logger("geo.request")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log each HTTP request with method, path, status, and duration."""

    # Paths to skip logging (high-frequency, no business value).
    _SKIP_PATHS: frozenset[str] = frozenset({"/health"})

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip health checks to avoid log noise.
        if request.url.path in self._SKIP_PATHS:
            return await call_next(request)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)

        # Use the path without query parameters to avoid leaking sensitive data.
        path = request.url.path

        _log.info(
            "request",
            method=request.method,
            path=path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response
