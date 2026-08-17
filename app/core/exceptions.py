"""Application-level exception hierarchy.

These exceptions are translated into HTTP responses by routers / middleware.
They intentionally avoid exposing internal stack traces or secrets.
"""

from __future__ import annotations


class AppError(Exception):
    """Base application error."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class AuthenticationError(AppError):
    status_code = 401
    code = "authentication_error"


class AuthorizationError(AppError):
    status_code = 403
    code = "authorization_error"


class TenantAccessError(AuthorizationError):
    """Raised when a user attempts to access a resource outside their tenant."""

    code = "tenant_access_denied"


class QuotaExceededError(AppError):
    status_code = 429
    code = "quota_exceeded"


class InfrastructureError(AppError):
    status_code = 503
    code = "infrastructure_unavailable"
