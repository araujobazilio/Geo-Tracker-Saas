"""Authentication API router.

Endpoints:
  POST /api/v1/auth/register  — create account + default workspace
  POST /api/v1/auth/login     — authenticate + issue session
  POST /api/v1/auth/logout    — revoke session + clear cookie
  GET  /api/v1/auth/me        — current user info
  GET  /api/v1/auth/csrf      — issue CSRF token to authenticated session
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Response, status

from app.config import Settings, get_settings
from app.core.exceptions import AuthenticationError, RateLimitExceededError
from app.dependencies import (
    get_auth_service,
    get_client_ip,
    get_current_user,
    get_rate_limiter,
    get_session_service,
    get_workspace_service,
    require_authenticated_user,
)
from app.models.user import User
from app.schemas.auth import (
    CsrfResponse,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    UserResponse,
    UserWithWorkspacesResponse,
    WorkspaceBrief,
)
from app.services.auth_service import AuthService
from app.services.rate_limiter import RateLimiter
from app.services.session_service import SessionService
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    """Set the HttpOnly session cookie with security attributes."""
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    """Clear the session cookie."""
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        httponly=True,
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(
    request: RegisterRequest,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    rate_limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
    client_ip: Annotated[str, Depends(get_client_ip)],
) -> UserResponse:
    """Register a new user and create a default personal workspace."""
    if not rate_limiter.check("register", client_ip):
        raise RateLimitExceededError("Too many registration attempts. Please try again later.")

    result = auth_service.register(request.email, request.password)
    _set_session_cookie(response, result.session_token, settings)
    return UserResponse.model_validate(result.user)


@router.post("/login", response_model=UserResponse)
def login(
    request: LoginRequest,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    rate_limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
    client_ip: Annotated[str, Depends(get_client_ip)],
) -> UserResponse:
    """Authenticate and issue a session cookie."""
    if not rate_limiter.check("login", client_ip):
        raise RateLimitExceededError("Too many login attempts. Please try again later.")

    result = auth_service.login(request.email, request.password)
    _set_session_cookie(response, result.session_token, settings)
    return UserResponse.model_validate(result.user)


@router.post("/logout", response_model=MessageResponse)
def logout(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
    user: Annotated[User | None, Depends(get_current_user)],
    geo_session: Annotated[str | None, Cookie(alias="geo_session")] = None,
) -> MessageResponse:
    """Revoke the session and clear the cookie. Idempotent."""
    _clear_session_cookie(response, settings)
    if geo_session:
        session_service.revoke_session(geo_session)
    return MessageResponse(message="Logged out.")


@router.get("/me", response_model=UserWithWorkspacesResponse)
def me(
    user: Annotated[User, Depends(require_authenticated_user)],
    ws_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> UserWithWorkspacesResponse:
    """Return the current authenticated user with workspace context."""
    workspaces = ws_service.list_workspaces(user.id)
    briefs = []
    for ws in workspaces:
        role = ws_service.get_user_role(ws.id, user.id)
        briefs.append(
            WorkspaceBrief(
                id=ws.id,
                name=ws.name,
                workspace_type=ws.workspace_type,
                role=role.value if role is not None else "MEMBER",
            )
        )
    return UserWithWorkspacesResponse(
        id=user.id,
        email=user.email,
        is_admin=user.is_admin,
        created_at=user.created_at,
        workspaces=briefs,
    )


@router.get("/csrf", response_model=CsrfResponse)
def get_csrf_token(
    user: Annotated[User, Depends(require_authenticated_user)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
    geo_session: Annotated[str | None, Cookie(alias="geo_session")] = None,
) -> CsrfResponse:
    """Return the CSRF token for the current session.

    The browser sends this token back via the X-CSRF-Token header on
    state-changing requests.
    """
    if geo_session is None:
        raise AuthenticationError("Authentication required.")
    session = session_service.get_session(geo_session)
    if session is None:
        raise AuthenticationError("Authentication required.")
    return CsrfResponse(csrf_token=session.csrf_token)
