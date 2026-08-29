"""Celery transport for Scan IDs; PostgreSQL remains authoritative state."""

from __future__ import annotations

from celery import Celery
from celery.schedules import schedule as celery_schedule

from app.config import get_settings

settings = get_settings()
celery_app = Celery(
    "geo_tracker",
    broker=settings.celery_broker_url or settings.redis_url,
    include=[
        "app.workers.scan_tasks",
        "app.workers.schedule_tasks",
        "app.workers.notification_tasks",
    ],
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
    # Celery Beat — periodic task schedule.
    # PostgreSQL remains authoritative; Beat only triggers the sweep.
    beat_schedule={
        "schedule-dispatch-due": {
            "task": "schedule.dispatch_due",
            "schedule": celery_schedule(run_every=settings.scheduler_sweep_interval_seconds),
        },
        "notification-dispatch-pending": {
            "task": "notification.dispatch_pending",
            "schedule": celery_schedule(run_every=settings.email_outbox_sweep_interval_seconds),
        },
        "notification-recover-stale-sending": {
            "task": "notification.recover_stale_sending",
            "schedule": celery_schedule(run_every=settings.email_outbox_sweep_interval_seconds),
        },
    },
)
