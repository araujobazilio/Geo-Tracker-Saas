"""Web-specific FastAPI dependencies.

These differ from the API dependencies in one key way: when a user is
not authenticated, web GET dependencies redirect to ``/login`` instead
of returning a JSON 401 error. API endpoints continue to use the
existing ``require_authenticated_user`` dependency.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.config import Settings, get_settings
from app.dependencies import get_current_user, get_session_service, get_workspace_auth_service
from app.models.user import User
from app.services.session_service import SessionService
from app.services.workspace_auth_service import WorkspaceAuthorizationService


def get_web_user(
    user: Annotated[User | None, Depends(get_current_user)],
) -> User | None:
    """Return the current user or None. Does NOT raise — the caller decides."""
    return user


def require_web_user(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)],
) -> User:
    """Require an authenticated user for web routes.

    If not authenticated, redirect to /login (for GET) or return 401
    (for HTMX/POST which should not redirect silently).
    """
    if user is not None:
        return user
    if request.method == "GET":
        raise HTTPException(
            status_code=302,
            headers={"Location": "/login?next=" + request.url.path},
        )
    from app.core.exceptions import AuthenticationError

    raise AuthenticationError("Authentication required.")


def get_web_csrf_token(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
) -> str:
    """Extract the CSRF token from the current session for template use."""
    cookie = request.cookies.get(settings.session_cookie_name)
    if cookie is None:
        return ""
    session = session_service.get_session(cookie)
    if session is None:
        return ""
    return session.csrf_token


def require_workspace_membership(
    workspace_id: uuid.UUID,
    user: Annotated[User, Depends(require_web_user)],
    auth_service: Annotated[WorkspaceAuthorizationService, Depends(get_workspace_auth_service)],
) -> WorkspaceAuthorizationService:
    """Verify workspace membership. Raises TenantAccessError (→404) if not a member."""
    auth_service.require_membership(workspace_id, user.id)
    return auth_service


def get_web_scan_dispatcher() -> ScanDispatcher:
    """FastAPI dependency for scan dispatcher injection.

    Production returns a CeleryScanDispatcher. Tests override this via
    ``app.dependency_overrides[get_web_scan_dispatcher] = lambda: fake``
    to inject a RecordingDispatcher without monkeypatching.
    """
    from app.services.scanning.dispatcher import CeleryScanDispatcher

    return CeleryScanDispatcher()


# Imported here for type-checking only; the actual class lives in the
# scanning package to avoid a circular import at module load time.
from app.services.scanning.dispatcher import ScanDispatcher  # noqa: E402
