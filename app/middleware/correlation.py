"""Request correlation ID middleware.

For every HTTP request:
- Accept a syntactically safe incoming X-Request-ID or generate one.
- Expose it in the response as X-Request-ID.
- Bind it into structlog contextvars for structured log correlation.

The correlation ID is a UUID4 hex string (32 chars, no dashes) to keep
it compact and URL-safe. Incoming X-Request-ID values are validated:
only alphanumeric, dash, and underscore characters are accepted, up
to 64 characters. This prevents log injection via newline/control chars.

No secrets, tokens, or credentials are ever included in the correlation ID.
"""

from __future__ import annotations

import re
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# Valid correlation ID: alphanumeric + dash + underscore, max 64 chars.
_VALID_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_REQUEST_ID_CTX_KEY = "request_id"


def _generate_request_id() -> str:
    """Generate a new correlation ID (UUID4 hex)."""
    return uuid.uuid4().hex


def _validate_incoming_request_id(value: str) -> str | None:
    """Return a sanitized incoming request ID or None if invalid."""
    if value and _VALID_REQUEST_ID_RE.match(value):
        return value
    return None


class RequestCorrelationMiddleware(BaseHTTPMiddleware):
    """Inject X-Request-ID into request, response, and structured logs."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("X-Request-ID", "")
        request_id = _validate_incoming_request_id(incoming) or _generate_request_id()

        # Bind into structlog context for all log entries in this request.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**{_REQUEST_ID_CTX_KEY: request_id})

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
