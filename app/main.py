"""FastAPI application entrypoint.

Creates the ASGI app, configures logging, CORS, and registers routers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.routers import infra


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

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(infra.router)
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
