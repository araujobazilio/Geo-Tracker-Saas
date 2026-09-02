"""Web authentication routes and shared cookie helpers.

The cookie-set/clear helpers are extracted here so both the API auth
router and the web auth router use the SAME implementation, avoiding
divergent security behavior.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.core.exceptions import (
    AuthenticationError,
    ConflictError,
    RateLimitExceededError,
    ValidationError,
)
from app.dependencies import (
    get_auth_service,
    get_client_ip,
    get_current_user,
    get_login_rate_limiter,
    get_register_rate_limiter,
)
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.rate_limiter import RateLimiter

router = APIRouter(tags=["web-auth"])


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    """Set the HttpOnly session cookie with security attributes.

    Shared between API and web auth — do not duplicate this logic.
    """
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
    """Clear the session cookie. Shared between API and web auth."""
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        httponly=True,
    )


def _safe_error(exc: Exception) -> str:
    """Map exceptions to customer-safe error messages (no internal details)."""
    if isinstance(exc, AuthenticationError):
        return "Invalid email or password."
    if isinstance(exc, ConflictError):
        return "An account with this email already exists."
    if isinstance(exc, ValidationError):
        return str(exc.message) if exc.message else "Please check your input and try again."
    if isinstance(exc, RateLimitExceededError):
        return "Too many attempts. Please try again later."
    return "Something went wrong. Please try again."


@router.get("/login")
def login_page(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)],
) -> Response:
    """Render the login page. Redirect to /app if already authenticated."""
    if user is not None:
        return RedirectResponse(url="/app", status_code=302)
    templates = Jinja2Templates(directory="app/templates")
    return templates.TemplateResponse(request, "auth/login.html")


@router.post("/login")
def login_submit(
    request: Request,
    response: Response,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    rate_limiter: Annotated[RateLimiter, Depends(get_login_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
    client_ip: Annotated[str, Depends(get_client_ip)],
) -> Response:
    """Process login form submission. Sets cookie and redirects on success."""
    templates = Jinja2Templates(directory="app/templates")
    if rate_limiter.is_limited("login", client_ip):
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {
                "error": "Too many login attempts. Please try again later.",
                "email": email,
            },
            status_code=429,
        )

    try:
        result = auth_service.login(email, password)
    except AuthenticationError:
        rate_limiter.record_failure("login", client_ip)
        if rate_limiter.is_limited("login", client_ip):
            msg = "Too many login attempts. Please try again later."
        else:
            msg = "Invalid email or password."
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {"error": msg, "email": email},
            status_code=401,
        )
    except Exception:
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {
                "error": "Something went wrong. Please try again.",
                "email": email,
            },
            status_code=500,
        )

    rate_limiter.reset("login", client_ip)
    set_session_cookie(response, result.session_token, settings)
    next_url = request.query_params.get("next", "/app")
    return RedirectResponse(url=next_url, status_code=302)


@router.get("/register")
def register_page(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user)],
) -> Response:
    """Render the registration page. Redirect to /app if already authenticated."""
    if user is not None:
        return RedirectResponse(url="/app", status_code=302)
    templates = Jinja2Templates(directory="app/templates")
    return templates.TemplateResponse(request, "auth/register.html")


@router.post("/register")
def register_submit(
    request: Request,
    response: Response,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    rate_limiter: Annotated[RateLimiter, Depends(get_register_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
    client_ip: Annotated[str, Depends(get_client_ip)],
) -> Response:
    """Process registration form submission. Sets cookie and redirects on success."""
    templates = Jinja2Templates(directory="app/templates")
    if not rate_limiter.check("register", client_ip):
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {
                "error": "Too many registration attempts. Please try again later.",
                "email": email,
            },
            status_code=429,
        )

    try:
        result = auth_service.register(email, password)
    except ConflictError:
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {
                "error": "An account with this email already exists.",
                "email": email,
            },
            status_code=409,
        )
    except ValidationError as exc:
        msg = exc.message if exc.message else "Please check your input and try again."
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {"error": msg, "email": email},
            status_code=422,
        )
    except Exception:
        return templates.TemplateResponse(
            request,
            "auth/register.html",
            {
                "error": "Something went wrong. Please try again.",
                "email": email,
            },
            status_code=500,
        )

    set_session_cookie(response, result.session_token, settings)
    return RedirectResponse(url="/app", status_code=302)


@router.post("/logout")
def logout_submit(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Logout: clear cookie, revoke session, redirect to /login."""
    from app.dependencies import get_session_service

    clear_session_cookie(response, settings)
    session_cookie = request.cookies.get(settings.session_cookie_name)
    if session_cookie:
        svc = get_session_service()
        svc.revoke_session(session_cookie)
    return RedirectResponse(url="/login", status_code=302)
