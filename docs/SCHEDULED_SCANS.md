# Scheduled Scans

## Overview

Scheduled scans automatically run recurring STANDARD scans on a Project at a fixed interval (e.g., every 24 hours). This eliminates the need for manual scan triggers and ensures the Action Center stays up to date.

## Key Principles

1. **PostgreSQL is authoritative.** Redis/Celery is transport only. Schedule state, scan lineage, and outcomes are all in PostgreSQL.
2. **No catch-up storm.** At most ONE due slot is handled per scheduler evaluation. `next_run_at` is advanced to the first future interval boundary regardless of outcome.
3. **Entitlement rechecked at execution time.** Entitlements can change after schedule creation. The scheduler rechecks `min_scheduled_scan_interval_hours` at execution time and skips if the plan no longer supports it.
4. **No engine duplication.** The scheduler creates STANDARD scans via `ScanCreationService` — the normal scan execution pipeline handles provider calls.
5. **Idempotent slots.** Each due slot has a deterministic idempotency key: `scheduled:{schedule_uuid}:{scheduled_for_iso}`.

## Entitlement Gate

`PlanDefinition.min_scheduled_scan_interval_hours` is the feature flag:
- `NULL` = scheduled scans unavailable on this plan.
- Positive integer = minimum permitted interval.

The schedule's `interval_hours` must be >= the current effective minimum at creation/update time AND at scheduler execution time.

## Scheduler Flow

1. **Beat task** `schedule.dispatch_due` runs every 60 seconds (configurable via `SCHEDULER_SWEEP_INTERVAL_SECONDS`).
2. **Claim** due schedules using `FOR UPDATE SKIP LOCKED` — multiple workers are safe.
3. **Recheck entitlement** — skip if plan no longer supports scheduled scans or interval is below minimum.
4. **Validate project** — skip if project is not ACTIVE.
5. **Check for active scan** — skip if a scheduled scan is already PENDING or RUNNING for this project.
6. **Create STANDARD scan** via `ScanCreationService` with `scan_schedule_id` and `scheduled_for` lineage.
7. **Advance `next_run_at`** to the first future interval boundary.

## Outcomes

Each scheduler evaluation produces a `ScheduledScanOutcome`:

| Outcome | Description |
|---------|-------------|
| `TRIGGERED` | Scan was created and dispatched. |
| `SKIPPED_ENTITLEMENT` | Plan doesn't support scheduled scans or interval below minimum. |
| `SKIPPED_PROJECT_INACTIVE` | Project is not ACTIVE. |
| `SKIPPED_ACTIVE_SCAN` | A scheduled scan is already in progress. |
| `SKIPPED_NOT_READY` | Project has no active PromptSet or other validation error. |
| `SKIPPED_QUOTA` | Insufficient monthly AI checks quota. |
| `DISPATCH_FAILED` | Scan creation failed unexpectedly. |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/workspaces/{ws}/projects/{pid}/schedule` | Get schedule |
| `PUT` | `/api/v1/workspaces/{ws}/projects/{pid}/schedule` | Create/replace schedule (ADMIN only) |
| `DELETE` | `/api/v1/workspaces/{ws}/projects/{pid}/schedule` | Disable schedule (ADMIN only) |

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `SCHEDULER_SWEEP_INTERVAL_SECONDS` | 60 | How often Beat sweeps for due schedules. |

## Docker Compose

The `beat` service runs Celery Beat, which triggers the scheduler sweep task periodically:

```yaml
beat:
  command: >
    celery -A app.workers.celery_app:celery_app beat
    --loglevel=INFO
```

## Automatic Action Center Refresh

After a scheduled scan reaches terminal status and `ScanAnalysis` is COMPLETED, the `ScheduledScanNotificationService` automatically runs `ActionGenerationService.refresh_from_scan()`. This updates the Action Center with the latest opportunities — no manual refresh needed.

This refresh is:
- **Local and deterministic** — zero AI Checks, zero provider calls.
- **Best-effort** — failures do NOT rollback the scan or analysis.
- **Idempotent** — running it multiple times produces the same result.
