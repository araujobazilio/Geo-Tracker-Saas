"""FastAPI dependencies for authentication and authorization.

Provides:
  - get_current_user: resolve User from session cookie (optional)
  - require_authenticated_user: require a valid authenticated User
  - get_session_service: SessionService factory
  - get_audit_service: AuditService factory
  - get_workspace_service: WorkspaceService factory
  - get_auth_service: AuthService factory
  - get_rate_limiter: RateLimiter factory
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Cookie, Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.exceptions import AuthenticationError
from app.db.redis import get_redis
from app.db.session import get_db
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.rate_limiter import RateLimiter
from app.services.session_service import SessionService
from app.services.workspace_service import WorkspaceService

# --- Service factories ---


def get_session_service() -> SessionService:
    settings = get_settings()
    return SessionService(redis=get_redis(), ttl_seconds=settings.session_ttl_seconds)


def get_audit_service() -> AuditService:
    return AuditService()


def get_auth_service(
    db: Annotated[Session, Depends(get_db)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
    audit_service: Annotated[AuditService, Depends(get_audit_service)],
) -> AuthService:
    return AuthService(session=db, session_service=session_service, audit_service=audit_service)


def get_workspace_service(
    db: Annotated[Session, Depends(get_db)],
    audit_service: Annotated[AuditService, Depends(get_audit_service)],
) -> WorkspaceService:
    return WorkspaceService(session=db, audit_service=audit_service)


def get_rate_limiter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> RateLimiter:
    return RateLimiter(
        redis=get_redis(),
        max_attempts=settings.rate_limit_login_max,
        window_seconds=settings.rate_limit_login_window_seconds,
    )


# --- Authentication dependencies ---


def get_current_user(
    db: Annotated[Session, Depends(get_db)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    session_cookie: Annotated[str | None, Cookie(alias="geo_session")] = None,
) -> User | None:
    """Resolve the current User from the session cookie, or None."""
    if session_cookie is None:
        return None
    session = session_service.get_session(session_cookie)
    if session is None:
        return None
    user_repo = UserRepository(db)
    try:
        user_id = uuid.UUID(session.user_id)
    except ValueError:
        return None
    user = user_repo.get_by_id(user_id)
    if user is None or not user.is_active:
        return None
    return user


def require_authenticated_user(
    user: Annotated[User | None, Depends(get_current_user)],
) -> User:
    """Require a valid authenticated User. Raises 401 if not authenticated."""
    if user is None:
        raise AuthenticationError("Authentication required.")
    return user


# --- Client IP resolution ---


def get_client_ip(request: Request) -> str:
    """Resolve the client IP address.

    For local development, uses request.client.host directly.
    Does NOT blindly trust X-Forwarded-For headers. Reverse proxy trust
    configuration should be hardened during production deployment.
    """
    if request.client is not None:
        return request.client.host
    return "unknown"
