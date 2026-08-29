"""Celery tasks for notification email delivery.

Tasks:
- ``notification.send_email`` — send one email delivery by ID.
- ``notification.dispatch_pending`` — sweeper for stranded PENDING deliveries.
- ``notification.recover_stale_sending`` — recover stale SENDING → FAILED.

PostgreSQL remains authoritative. The task loads the row, claims it
safely under a lock, sends through EmailTransport, and persists the
result.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.enums import EmailDeliveryStatus
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.models.email_delivery import EmailDelivery
from app.models.notification import Notification
from app.services.email_templates import render_email
from app.services.email_transport import build_email_transport
from app.workers.celery_app import celery_app

logger = get_logger("app.notification_tasks")


@celery_app.task(name="notification.send_email")
def send_email_task(email_delivery_id: str) -> dict[str, str]:
    """Send one email delivery by ID.

    Claims the row safely (PENDING → SENDING), sends via transport,
    persists SENT or FAILED. Two workers cannot send the same email
    simultaneously.
    """
    delivery_uuid = uuid.UUID(email_delivery_id)
    factory = get_session_factory()

    with factory() as session:
        # Claim under lock: PENDING → SENDING.
        row = (
            session.execute(
                select(EmailDelivery)
                .where(EmailDelivery.id == delivery_uuid)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .first()
        )

        if row is None:
            return {"status": "not_found"}

        if row.status == EmailDeliveryStatus.SENT:
            return {"status": "already_sent"}

        if row.status == EmailDeliveryStatus.FAILED:
            return {"status": "already_failed"}

        if row.status == EmailDeliveryStatus.SENDING:
            # Another worker is processing — skip.
            return {"status": "in_progress"}

        # Claim: PENDING → SENDING.
        row.status = EmailDeliveryStatus.SENDING
        row.attempt_count += 1
        row.last_attempt_at = datetime.now(UTC)
        session.commit()

    # Send outside the lock to avoid holding it during network I/O.
    try:
        _do_send(delivery_uuid, factory)
    except Exception as exc:
        logger.error("email_send_exception", delivery_id=str(delivery_uuid), error=str(exc))
        _mark_failed(delivery_uuid, factory, "EXCEPTION", str(exc)[:500])

    return {"status": "processed"}


def _do_send(delivery_uuid: uuid.UUID, factory: Any) -> None:
    """Load notification + delivery, render, send, persist result."""
    with factory() as session:
        delivery = session.get(EmailDelivery, delivery_uuid)
        if delivery is None:
            return

        notification = session.get(Notification, delivery.notification_id)
        if notification is None:
            _mark_failed(delivery_uuid, factory, "NOTIFICATION_GONE", "Notification deleted")
            return

        subject, text_body, html_body = render_email(notification)

        from app.config import get_settings

        settings = get_settings()

        transport = build_email_transport()
        result = transport.send(
            recipient=delivery.recipient_email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            from_address=settings.email_from_address,
            from_name=settings.email_from_name,
            message_id=delivery.message_id or f"<{delivery.id}@geo-tracker>",
        )

        if result.success:
            delivery.status = EmailDeliveryStatus.SENT
            delivery.sent_at = datetime.now(UTC)
            delivery.message_id = result.message_id
            delivery.failure_code = None
            delivery.failure_message = None
        else:
            delivery.status = EmailDeliveryStatus.FAILED
            delivery.failure_code = result.failure_code
            delivery.failure_message = result.failure_message

        session.commit()


def _mark_failed(
    delivery_uuid: uuid.UUID,
    factory: Any,
    code: str,
    message: str,
) -> None:
    """Mark a delivery as FAILED."""
    with factory() as session:
        delivery = session.get(EmailDelivery, delivery_uuid)
        if delivery is None:
            return
        delivery.status = EmailDeliveryStatus.FAILED
        delivery.failure_code = code
        delivery.failure_message = message
        session.commit()


@celery_app.task(name="notification.dispatch_pending")
def dispatch_pending_task() -> dict[str, int]:
    """Sweeper for stranded PENDING email deliveries.

    Enqueues ``notification.send_email`` for PENDING rows older than
    a small safety interval. Does NOT send directly. Provides
    broker-outage self-healing.
    """
    from app.config import get_settings

    settings = get_settings()
    threshold = datetime.now(UTC) - timedelta(seconds=settings.email_outbox_sweep_interval_seconds)
    factory = get_session_factory()
    count = 0

    with factory() as session:
        rows = (
            session.execute(
                select(EmailDelivery)
                .where(
                    EmailDelivery.status == EmailDeliveryStatus.PENDING,
                    EmailDelivery.created_at < threshold,
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
            .scalars()
            .all()
        )

        for row in rows:
            try:
                send_email_task.delay(str(row.id))
                count += 1
            except Exception:
                logger.warning("dispatch_pending_failed", delivery_id=str(row.id))

    return {"dispatched": count}


@celery_app.task(name="notification.recover_stale_sending")
def recover_stale_sending_task() -> dict[str, int]:
    """Recover stale SENDING email deliveries → FAILED.

    A worker may die after claiming SENDING before completing. Because
    SMTP send outcome may be ambiguous, the safe MVP is to mark stale
    SENDING as FAILED, requiring manual retry. This avoids automatic
    duplicate email.
    """
    from app.config import get_settings

    settings = get_settings()
    threshold = datetime.now(UTC) - timedelta(
        seconds=settings.email_stale_sending_threshold_seconds
    )
    factory = get_session_factory()
    count = 0

    with factory() as session:
        rows = (
            session.execute(
                select(EmailDelivery)
                .where(
                    EmailDelivery.status == EmailDeliveryStatus.SENDING,
                    EmailDelivery.last_attempt_at < threshold,
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
            .scalars()
            .all()
        )

        for row in rows:
            row.status = EmailDeliveryStatus.FAILED
            row.failure_code = "STALE_SENDING"
            row.failure_message = "Worker died during SENDING; manual retry required."
            count += 1

        session.commit()

    return {"recovered": count}
