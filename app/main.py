"""FastAPI application entrypoint.

Creates the ASGI app, configures logging, CORS, CSRF middleware,
exception handlers, and registers routers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.core.csrf import CSRFMiddleware
from app.core.exceptions import AppError, TenantAccessError
from app.core.logging import configure_logging, get_logger
from app.routers import infra
from app.routers.api import analysis as analysis_router
from app.routers.api import auth as auth_router
from app.routers.api import competitor_explanation as competitor_explanation_router
from app.routers.api import confidence as confidence_router
from app.routers.api import entitlements as entitlements_router
from app.routers.api import notifications as notifications_router
from app.routers.api import opportunities as opportunities_router
from app.routers.api import projects as projects_router
from app.routers.api import scans as scans_router
from app.routers.api import schedule as schedule_router
from app.routers.api import verification as verification_router
from app.routers.api import workspaces as workspaces_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: configure logging on startup."""
    configure_logging()
    logger = get_logger("app.lifespan")
    settings = get_settings()
    logger.info(
        "application_starting",
        app_env=settings.app_env,
        app_name=settings.app_name,
    )
    try:
        yield
    finally:
        logger.info("application_stopping", app_env=settings.app_env)


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="AI Visibility Intelligence platform",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # CORS — never allow_credentials with wildcard origins.
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # CSRF protection for state-changing requests.
    app.add_middleware(CSRFMiddleware)

    # Centralized exception handlers.
    # For web (HTML) routes, return HTML error pages. For API (JSON) routes,
    # return JSON error responses. We distinguish by Accept header.
    _templates = Jinja2Templates(directory="app/templates")

    def _is_web_request(request: Request) -> bool:
        """Return True if the client expects HTML (web browser), not JSON."""
        accept = request.headers.get("accept", "")
        return "text/html" in accept and "application/json" not in accept

    @app.exception_handler(TenantAccessError)
    async def tenant_access_handler(
        request: Request, exc: TenantAccessError
    ) -> JSONResponse | HTMLResponse:
        # Return 404 to avoid revealing whether an inaccessible resource exists.
        if _is_web_request(request):
            return _templates.TemplateResponse(request, "errors/404.html", {}, status_code=404)
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": "Resource not found."}},
        )

    @app.exception_handler(AppError)
    async def app_error_handler(
        request: Request, exc: AppError
    ) -> JSONResponse | HTMLResponse | RedirectResponse:
        if _is_web_request(request):
            status_code = exc.status_code
            if status_code == 404:
                return _templates.TemplateResponse(request, "errors/404.html", {}, status_code=404)
            if status_code == 403:
                return _templates.TemplateResponse(request, "errors/403.html", {}, status_code=403)
            if status_code == 401:
                return RedirectResponse(url="/login", status_code=302)
            # Expected customer-facing errors (409/422/429): render a safe
            # message page without exposing internal details.
            if status_code in (409, 422, 429):
                return _templates.TemplateResponse(
                    request,
                    "errors/4xx.html",
                    {"status_code": status_code, "message": exc.message},
                    status_code=status_code,
                )
            # 500 and everything else: generic error page.
            return _templates.TemplateResponse(request, "errors/500.html", {}, status_code=500)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    # Infrastructure endpoints (unversioned).
    app.include_router(infra.router)
    # API v1 routers.
    app.include_router(auth_router.router)
    app.include_router(workspaces_router.router)
    app.include_router(entitlements_router.router)
    app.include_router(projects_router.router)
    app.include_router(scans_router.router)
    app.include_router(analysis_router.router)
    app.include_router(confidence_router.router)
    app.include_router(competitor_explanation_router.router)
    app.include_router(opportunities_router.router)
    app.include_router(opportunities_router.scan_router)
    app.include_router(verification_router.router)
    app.include_router(schedule_router.router)
    app.include_router(notifications_router.router)

    # Web application routes (customer-facing Jinja2 + HTMX).
    from app.web.router import web_router

    app.include_router(web_router)

    # Static files (CSS, JS, vendored HTMX + Chart.js).
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()


def run() -> None:
    """Run the application with uvicorn (entrypoint script)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=not settings.is_production,
    )


if __name__ == "__main__":
    run()
