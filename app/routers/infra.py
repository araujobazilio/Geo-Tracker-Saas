"""Infrastructure health endpoints.

- GET /health  → liveness, no external dependencies
- GET /ready   → readiness, verifies PostgreSQL + Redis

These endpoints are intentionally outside /api/v1 versioning.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app.db.redis import check_redis
from app.db.session import check_database

router = APIRouter(tags=["infra"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. Never touches external infrastructure."""
    return {"status": "ok"}


@router.get("/ready")
def ready() -> JSONResponse:
    """Readiness probe. Verifies PostgreSQL and Redis connectivity."""
    db_ok = check_database()
    redis_ok = check_redis()

    payload: dict[str, str] = {
        "status": "ready" if (db_ok and redis_ok) else "not_ready",
        "database": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
    }

    code = status.HTTP_200_OK if (db_ok and redis_ok) else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content=payload)
