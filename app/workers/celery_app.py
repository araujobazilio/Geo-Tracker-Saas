"""Celery transport for Scan IDs; PostgreSQL remains authoritative state."""

from __future__ import annotations

from celery import Celery

from app.config import get_settings

settings = get_settings()
celery_app = Celery(
    "geo_tracker",
    broker=settings.celery_broker_url or settings.redis_url,
    include=["app.workers.scan_tasks"],
)
celery_app.conf.update(
    task_acks_late=False,
    task_ignore_result=True,
    result_backend=None,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)
