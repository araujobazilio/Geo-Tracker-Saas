"""Shared session cookie helpers.

Single implementation used by both the API auth router and the web auth
router to ensure identical security behavior.
"""

from __future__ import annotations

from fastapi import Response

from app.config import Settings


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
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


def clear_session_cookie(response: Response, settings: Settings) -> None:
    """Clear the session cookie."""
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        httponly=True,
    )
