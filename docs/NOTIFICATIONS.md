# Notifications

## Overview

Notifications inform users about important events: scan completion, new opportunities, verification results, and scheduled scan outcomes. Each notification is deduplicated by a `dedup_key` to avoid spamming users with repeated alerts.

## Notification Types

| Type | Description |
|------|-------------|
| `SCAN_COMPLETED` | A scan reached terminal status (COMPLETED or FAILED). |
| `SCAN_FAILED` | A scan failed unexpectedly. |
| `NEW_OPPORTUNITIES` | New opportunities were detected in the Action Center. |
| `VERIFICATION_COMPLETED` | A verification cycle completed. |
| `VERIFICATION_FAILED` | A verification cycle failed. |
| `SCHEDULED_SCAN_TRIGGERED` | A scheduled scan was triggered. |
| `SCHEDULED_SCAN_SKIPPED` | A scheduled scan was skipped (entitlement, project, quota). |
| `SCHEDULED_SCAN_DISPATCH_FAILED` | A scheduled scan dispatch failed. |

## Deduplication

Each notification has a `dedup_key` (unique within a workspace). If a notification with the same `dedup_key` already exists, it is NOT recreated. This prevents duplicate notifications for the same event.

Example: `scan:{scan_id}:completed` — only one "scan completed" notification per scan.

## Notification Preferences

Users can configure which notification types they want to receive:

| Preference | Default | Description |
|------------|---------|-------------|
| `notify_scan_completed` | `True` | Scan completion notifications. |
| `notify_scan_failed` | `True` | Scan failure notifications. |
| `notify_new_opportunities` | `True` | New opportunities notifications. |
| `notify_verification_completed` | `True` | Verification completion notifications. |
| `notify_verification_failed` | `True` | Verification failure notifications. |
| `notify_scheduled_scan_triggered` | `False` | Scheduled scan triggered (off by default — too noisy). |
| `notify_scheduled_scan_skipped` | `False` | Scheduled scan skipped (off by default). |
| `notify_scheduled_scan_dispatch_failed` | `True` | Scheduled scan dispatch failed. |
| `email_enabled` | `True` | Master switch for email delivery. |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/workspaces/{ws}/notifications` | List notifications (paginated) |
| `GET` | `/api/v1/workspaces/{ws}/notifications/unread-count` | Get unread count |
| `POST` | `/api/v1/workspaces/{ws}/notifications/{id}/mark-read` | Mark single notification read |
| `POST` | `/api/v1/workspaces/{ws}/notifications/mark-all-read` | Mark all notifications read |
| `GET` | `/api/v1/workspaces/{ws}/notifications/preferences` | Get notification preferences |
| `PUT` | `/api/v1/workspaces/{ws}/notifications/preferences` | Update notification preferences |

## Integration Points

Notifications are generated at these points:

1. **Scan Finalization** (`ScanFinalizationService`) — after a scan reaches terminal status.
2. **Verification Evaluation** (`VerificationEvaluationService`) — after a verification cycle completes.
3. **Scheduled Scan Evaluation** (`ScheduledScanNotificationService`) — after a scheduled scan is triggered or skipped.

## Outbox Pattern

Notifications use an outbox pattern for email delivery:

1. Notification is created in the database (same transaction as the triggering event).
2. If the user has email enabled for this notification type, an `EmailDelivery` record is created.
3. A Celery task (`notification.dispatch_outbox`) processes pending `EmailDelivery` records.
4. Each email is sent via SMTP (or memory transport in tests).
5. `EmailDelivery.status` tracks: `PENDING` → `SENT` / `FAILED`.
