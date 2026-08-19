"""FastAPI application entrypoint.

Creates the ASGI app, configures logging, CORS, CSRF middleware,
exception handlers, and registers routers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.core.csrf import CSRFMiddleware
from app.core.exceptions import AppError, TenantAccessError
from app.core.logging import configure_logging, get_logger
from app.routers import infra
from app.routers.api import auth as auth_router
from app.routers.api import entitlements as entitlements_router
from app.routers.api import projects as projects_router
from app.routers.api import scans as scans_router
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
    @app.exception_handler(TenantAccessError)
    async def tenant_access_handler(request: Request, exc: TenantAccessError) -> JSONResponse:
        # Return 404 to avoid revealing whether an inaccessible resource exists.
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": "Resource not found."}},
        )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
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
