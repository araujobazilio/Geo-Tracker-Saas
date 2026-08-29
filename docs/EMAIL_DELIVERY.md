# Email Delivery

## Overview

Email delivery uses an outbox pattern to reliably send notification emails via SMTP. The `EmailDelivery` model tracks each email's lifecycle from creation to sent/failed.

## Architecture

```
Notification created
    ↓
EmailDelivery record created (PENDING)
    ↓
Celery task: notification.dispatch_outbox
    ↓
SMTPEmailTransport.send()
    ↓
EmailDelivery.status = SENT (or FAILED)
```

## Email Transport

The `EmailTransport` protocol defines a single `send()` method:

```python
class EmailTransport(Protocol):
    def send(
        self,
        *,
        to: str,
        subject: str,
        html_body: str,
        text_body: str | None = None,
    ) -> None: ...
```

Two implementations:

| Implementation | Description |
|----------------|-------------|
| `SMTPEmailTransport` | Production SMTP client. Connects to SMTP server with TLS. |
| `MemoryEmailTransport` | In-memory transport for tests. Stores sent emails in a list. |

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `SMTP_HOST` | (empty) | SMTP server hostname. |
| `SMTP_PORT` | `587` | SMTP server port. |
| `SMTP_USERNAME` | (empty) | SMTP username. |
| `SMTP_PASSWORD` | (empty) | SMTP password. |
| `SMTP_FROM_EMAIL` | `noreply@example.com` | From email address. |
| `SMTP_FROM_NAME` | `Geo Tracker` | From display name. |
| `SMTP_USE_TLS` | `True` | Use STARTTLS. |
| `EMAIL_ENABLED` | `False` | Master switch for email delivery. |

When `EMAIL_ENABLED=False` or `SMTP_HOST` is empty, email delivery is silently skipped (no errors, no retries). This is useful for development and testing.

## Email Templates

Templates are defined in `app/services/email_templates.py` as plain Python functions that return `(subject, html_body, text_body)` tuples. No external template engine is used — this keeps the dependency surface minimal and templates deterministic.

Available templates:

| Template | Trigger |
|----------|---------|
| `scan_completed` | Scan reaches COMPLETED status. |
| `scan_failed` | Scan reaches FAILED status. |
| `new_opportunities` | New opportunities detected. |
| `verification_completed` | Verification cycle completed. |
| `verification_failed` | Verification cycle failed. |
| `scheduled_scan_triggered` | Scheduled scan triggered. |
| `scheduled_scan_skipped` | Scheduled scan skipped. |
| `scheduled_scan_dispatch_failed` | Scheduled scan dispatch failed. |

## Outbox Processing

The Celery task `notification.dispatch_outbox` runs periodically (every 30 seconds by default) and:

1. Queries `EmailDelivery` records with `status=PENDING`.
2. For each record, loads the associated `Notification` and `User`.
3. Renders the email using the appropriate template.
4. Sends via `EmailTransport.send()`.
5. Updates `EmailDelivery.status` to `SENT` or `FAILED`.
6. Records `sent_at` or `error_message`.

Failed emails are NOT retried automatically — they remain in `FAILED` status for manual inspection. This prevents infinite retry loops for permanently failed emails (e.g., invalid address).

## Docker Compose

The `worker` service processes the email outbox:

```yaml
worker:
  command: >
    celery -A app.workers.celery_app:celery_app worker
    --loglevel=INFO
    --concurrency=2
```
