"""Web router aggregation — mounts all web sub-routers.

This keeps the web layer modular without a monolithic router file.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.web.auth import router as auth_router
from app.web.notifications import router as notifications_router
from app.web.onboarding import router as onboarding_router
from app.web.opportunities import router as opportunities_router
from app.web.pages import router as pages_router
from app.web.project_config import router as project_config_router
from app.web.scans import router as scans_router
from app.web.schedule import router as schedule_router

web_router = APIRouter()

# Auth routes (login, register, logout)
web_router.include_router(auth_router)
# Guided onboarding wizard (must be before pages_router so /projects/new
# doesn't match /projects/{project_id})
web_router.include_router(onboarding_router)
# Root + workspace + project dashboard pages
web_router.include_router(pages_router)
# Scan execution, polling, detail
web_router.include_router(scans_router)
# Action Center (opportunities)
web_router.include_router(opportunities_router)
# Schedule management
web_router.include_router(schedule_router)
# Notification center + preferences
web_router.include_router(notifications_router)
# Project configuration (prompt regeneration)
web_router.include_router(project_config_router)
