# Operations

Operations runbook for the GEO Tracker production stack
(`docker-compose.prod.yml`). Services: `nginx`, `app`, `worker`, `beat`,
`postgres`, `redis`, plus a one-shot `migrate` container.

All commands assume you are in the repository root on the VPS with
`GIT_SHA` exported and `docker compose -f docker-compose.prod.yml` as
the base invocation. For brevity, the examples below abbreviate this as
`dc`.

```bash
alias dc='docker compose -f docker-compose.prod.yml'
```

## Where to check logs

Each service writes structured logs to stdout/stderr, captured by
Docker. Use `docker compose logs` with `-f` to follow and `--since` to
limit scope.

### Web (app) logs

```bash
dc logs -f app
dc logs --since 15m app
dc logs --since 10m app | grep ERROR
```

The app emits structured JSON-ish log events with a `request_id`
correlation ID (see `app/middleware/correlation.py`). To trace a single
request, grep for its `X-Request-ID`:

```bash
dc logs app | grep '"request_id":"7f3c1a2b...'
```

### Worker logs

```bash
dc logs -f worker
dc logs --since 30m worker | grep -E 'scan\.execute|schedule\.dispatch_due|notification\.'
```

The worker runs `celery -A app.workers.celery_app:celery_app worker
--loglevel=INFO --concurrency=1`. Scan execution, scheduled-scan
dispatch, and email delivery all run inside this one worker process.

### Beat logs

```bash
dc logs -f beat
dc logs --since 1h beat | grep -E 'schedule|notification'
```

Beat emits a line each time it fires a periodic task
(`schedule.dispatch_due`, `notification.dispatch_pending`,
`notification.recover_stale_sending`). Use it to confirm the scheduler
is alive.

## Infrastructure health

### PostgreSQL health

The compose healthcheck already runs `pg_isready`. To check manually:

```bash
dc exec postgres pg_isready -U "${DATABASE_USER}" -d "${DATABASE_NAME}"
# expect: /var/run/postgresql:5432 - accepting connections
```

A connection count sanity check (compare against the connection budget
in `docs/DEPLOYMENT.md`):

```bash
dc exec postgres psql -U "${DATABASE_USER}" -d "${DATABASE_NAME}" -c \
  "SELECT state, count(*) FROM pg_stat_activity GROUP BY state;"
```

### Redis health

The compose healthcheck runs `redis-cli ping`:

```bash
dc exec redis redis-cli ping
# expect: PONG
```

Redis holds sessions and rate-limit counters only (see
`docs/BACKUP_AND_RESTORE.md`). It is safe to lose; it is not backed up.

### Readiness check

`/ready` verifies that the app can reach PostgreSQL and Redis. It
returns `503` if either dependency is down:

```bash
curl -fsS https://app.example.com/ready
# expect: {"status":"ready","database":"ok","redis":"ok",...}
```

`/health` is a liveness probe that touches no external dependencies —
use it only to confirm the process is up and to read build metadata
(`version`, `git_sha`, `build_time`):

```bash
curl -fsS https://app.example.com/health
```

## Worker health

Confirm the Celery worker process is responsive to the broker:

```bash
dc exec worker celery -A app.workers.celery_app:celery_app inspect ping
# expect: -> pong: {'ok': 'pong'}
```

A timeout or empty response means the worker is hung or the broker
(Redis DB 1) is unreachable. Check `dc logs worker` and `dc exec redis
redis-cli ping`.

To list registered tasks (confirms the worker loaded the scan,
schedule, and notification task modules):

```bash
dc exec worker celery -A app.workers.celery_app:celery_app inspect registered
```

## Beat singleton

There is **exactly ONE** `beat` container in the production stack. Celery
Beat is a singleton by design — running two beat instances would
duplicate every periodic task (double scan dispatch, double email
sweeps). The compose file defines a single `beat` service with
`restart: unless-stopped`.

Operational rules:

- Never scale `beat` with `--scale beat=2`. If you need redundancy, use
  a single beat behind a process supervisor — do not run two beat
  containers.
- If `beat` is restarting in a loop, periodic tasks stop firing. Stale
  scans and stranded emails will accumulate. Check `dc logs beat` and
  the broker URL (`redis://redis:6379/1`).
- The beat schedule (from `app/workers/celery_app.py`):
  - `schedule.dispatch_due` — every `SCHEDULER_SWEEP_INTERVAL_SECONDS`
    (default 60s).
  - `notification.dispatch_pending` — every
    `EMAIL_OUTBOX_SWEEP_INTERVAL_SECONDS` (default 60s).
  - `notification.recover_stale_sending` — same cadence as above.

PostgreSQL remains authoritative for all scheduled state; Beat only
triggers the sweeps. A beat outage does not lose scheduled scans — it
only delays them until beat recovers.

## Routine data checks

Run these SQL queries against the production database to spot stuck
work before users notice. Replace timestamps as needed.

### Recent failed Scans

```sql
SELECT id, workspace_id, project_id, scan_type, status,
       successful_runs, failed_runs, planned_ai_checks,
       completed_at, created_at
FROM scans
WHERE status IN ('FAILED', 'PARTIAL')
  AND completed_at >= now() - interval '24 hours'
ORDER BY completed_at DESC
LIMIT 50;
```

### Stale RUNNING / PENDING scans (past the stale threshold)

The default stale threshold is `SCAN_STALE_AFTER_SECONDS` (7200s / 2h).
`ScanRecoveryService` fails these without replaying provider calls.

```sql
SELECT id, workspace_id, project_id, scan_type, status,
       started_at, created_at,
       EXTRACT(EPOCH FROM (now() - COALESCE(started_at, created_at))) AS seconds_stale
FROM scans
WHERE status IN ('RUNNING', 'PENDING')
  AND COALESCE(started_at, created_at) < now() - interval '2 hours'
ORDER BY COALESCE(started_at, created_at) ASC;
```

### Pending / stale quota reservations

ACTIVE reservations hold real quota. A long-lived ACTIVE reservation
with `committed < reserved` indicates a scan that never finalized.

```sql
SELECT id, workspace_id, project_id, status,
       ai_checks_reserved, ai_checks_committed,
       expires_at, created_at
FROM quota_reservations
WHERE status = 'ACTIVE'
  AND ai_checks_committed < ai_checks_reserved
  AND created_at < now() - interval '6 hours'
ORDER BY created_at ASC;
```

EXPIRED reservations that still show `committed < reserved` should be
rare — investigate if the count grows:

```sql
SELECT count(*) AS expired_with_uncommitted
FROM quota_reservations
WHERE status = 'EXPIRED'
  AND ai_checks_committed < ai_checks_reserved;
```

### Pending / stale email deliveries

PENDING deliveries older than the sweep interval should be picked up by
`notification.dispatch_pending`. A growing backlog means the worker or
beat is down.

```sql
SELECT status, count(*),
       min(created_at) AS oldest,
       max(created_at) AS newest
FROM email_deliveries
WHERE status IN ('PENDING', 'SENDING')
GROUP BY status;
```

Stale SENDING (worker died mid-send) — these are recovered to FAILED by
`notification.recover_stale_sending` after
`EMAIL_STALE_SENDING_THRESHOLD_SECONDS` (default 300s):

```sql
SELECT id, notification_id, recipient_email, status,
       attempt_count, last_attempt_at, failure_code
FROM email_deliveries
WHERE status = 'SENDING'
  AND last_attempt_at < now() - interval '5 minutes'
ORDER BY last_attempt_at ASC;
```

Recently FAILED deliveries (may need manual retry):

```sql
SELECT id, recipient_email, failure_code, failure_message,
       attempt_count, last_attempt_at
FROM email_deliveries
WHERE status = 'FAILED'
  AND last_attempt_at >= now() - interval '24 hours'
ORDER BY last_attempt_at DESC
LIMIT 50;
```

### Scheduled scan outcomes

The last outcome of each project schedule:

```sql
SELECT pss.project_id, pss.enabled, pss.interval_hours,
       pss.next_run_at, pss.last_triggered_at, pss.last_outcome,
       pss.last_skip_reason, pss.last_scan_id
FROM project_scan_schedules pss
ORDER BY pss.next_run_at ASC;
```

Schedules that are due but have not triggered (beat or worker problem):

```sql
SELECT project_id, next_run_at, last_triggered_at, last_outcome
FROM project_scan_schedules
WHERE enabled = true
  AND next_run_at < now()
ORDER BY next_run_at ASC;
```

Outcome distribution over the last 24h (look for a spike in `SKIPPED`
or `ERROR`):

```sql
SELECT last_outcome, count(*)
FROM project_scan_schedules
WHERE last_triggered_at >= now() - interval '24 hours'
GROUP BY last_outcome;
```

## Incident runbooks

### Provider failure

Symptom: scans end `PARTIAL` or `FAILED` with provider error codes in
`prompt_runs.error_code`; user-visible "provider unavailable" messages.

1. Check which provider is failing:
   ```sql
   SELECT error_code, error_message, count(*)
   FROM prompt_runs
   WHERE status = 'FAILED'
     AND completed_at >= now() - interval '1 hour'
   GROUP BY error_code, error_message
   ORDER BY count(*) DESC
   LIMIT 20;
   ```
2. Confirm the provider API key is valid and the provider is up
   (status page). Check `dc logs worker | grep -i provider`.
3. Provider failures are **non-retriable by design** — the Scan Engine
   does not retry billable calls (see `docs/SCAN_ENGINE.md`). Do not
   manually re-dispatch failed runs; users can start a new scan.
4. If a provider is down for an extended period, consider pausing
   scheduled scans for affected projects or disabling the provider in
   plan configuration so new scans fail fast at entitlement time rather
   than after dispatch.
5. Unused quota for failed scans is released atomically at finalization
   — no manual quota cleanup is needed.

### Worker crash

Symptom: `dc logs worker` shows a traceback; scans stuck in `RUNNING`
or `PENDING` past `SCAN_STALE_AFTER_SECONDS`.

1. Confirm the worker container restarted (`restart: unless-stopped`
   handles this automatically):
   ```bash
   dc ps worker
   dc exec worker celery -A app.workers.celery_app:celery_app inspect ping
   ```
2. Stale scans are recovered automatically by
   `ScanRecoveryService.recover_stale_scans()`, which is invoked from
   the scan execution path and finalization. Recovery **never replays
   provider requests** — it marks unresolved `PromptRun`s as FAILED and
   finalizes the scan (releasing unused quota). See
   `app/services/scan_finalization_service.py`.
3. If recovery has not run (e.g. beat is also down), the next
   finalization or scan execution will reconcile stranded scans. You
   can also trigger a fresh scan on an affected project to nudge
   recovery, but do not manually rewrite scan rows.
4. For VERIFICATION scans, recovery additionally calls
   `reconcile_verification_lifecycle()` so the
   `OpportunityVerification` is not left stranded PENDING — zero
   provider replay, zero new `UsageEvents`.

### Quota incident

Symptom: a workspace cannot start scans despite appearing to have
quota; or `quota_reservations` shows stranded ACTIVE rows.

1. Run the pending/stale quota reservation query above.
2. Stranded ACTIVE reservations are released idempotently by
   `ScanFinalizationService._reconcile_terminal_scan()` the next time
   the related scan is finalized. If the scan is already terminal,
   forcing a no-op finalize path is safe — it releases remaining
   reserved checks without provider calls or new `UsageEvents`.
3. Never manually delete `quota_reservations` rows — they are
   accounting history. Never manually decrement
   `workspace_usage_periods.ai_checks_used`; the release path is the
   only correct way to return quota.
4. If a workspace is genuinely out of quota, that is expected behavior,
   not an incident — communicate the plan limit to the user.

### Email incident

Symptom: notifications not arriving; `email_deliveries` backlog grows.

1. Run the pending/stale email delivery queries above.
2. Confirm SMTP is reachable from the worker:
   ```bash
   dc exec worker sh -c 'echo QUIT | nc -w 5 ${SMTP_HOST} ${SMTP_PORT}'
   ```
3. PENDING backlog → `notification.dispatch_pending` (beat) should be
   enqueuing sends every 60s. If it is not, check beat is running.
4. Stale SENDING → `notification.recover_stale_sending` marks them
   FAILED after 5 minutes to avoid duplicate emails. FAILED deliveries
   require **manual retry** — there is no automatic re-send, by design,
   because SMTP send outcome can be ambiguous and duplicate emails are
   worse than a delayed one.
5. To manually retry a FAILED delivery, re-enqueue the task:
   ```bash
   dc exec worker celery -A app.workers.celery_app:celery_app call \
     notification.send_email --args='"<email_delivery_id>"'
   ```
   The task is idempotent: a row already `SENT` returns
   `already_sent`; a `FAILED` row is re-claimed and retried.

## Celery worker concurrency

For closed beta, the production worker runs with **`--concurrency=1`**
(see `docker-compose.prod.yml`). This is deliberate and conservative:

- Scan execution makes at most one billable provider call per `PromptRun`
  and never retries it. A single concurrency slot serializes provider
  calls, making cost and rate-limit behavior predictable.
- `worker_prefetch_multiplier=1` (set in `celery_app.py`) means the
  worker does not hoard tasks, so a crash only ever loses the one
  in-flight task — which `ScanRecoveryService` then fails safely.
- A single VPS with 4 GB RAM cannot safely run multiple concurrent
  scan executions alongside PostgreSQL and Redis.

Raise concurrency only after load testing and only if provider rate
limits and the connection budget (see `docs/DEPLOYMENT.md`) allow it.
Each additional concurrency slot adds up to
`db_pool_size + db_max_overflow` PostgreSQL connections.

## Resource guidance for a single VPS

| Component        | Setting                                         | Notes |
|------------------|-------------------------------------------------|-------|
| app (uvicorn)    | `--workers 2`                                   | 2 workers is conservative for a 2 vCPU box. |
| worker (celery)  | `--concurrency=1`                               | See above. |
| beat             | 1 instance                                      | Singleton. Never scale. |
| PostgreSQL       | default `max_connections=100`                   | Steady-state ceiling ~60; see connection budget. |
| Redis            | `--appendonly yes`                              | Persistence for sessions/rate limits; safe to lose. |
| Nginx            | `client_max_body_size 10m`, 60s timeouts        | See `docker/nginx/nginx.conf`. |

Monitoring essentials:

- Disk usage on the PostgreSQL volume — a full disk corrupts the
  database. Alert at 75%.
- Container restart counts (`dc ps`). Repeated `app`/`worker` restarts
  indicate OOM or crashes.
- `/ready` from an external prober (UptimeRobot, healthchecks.io) —
  alerts on `503` before users report an outage.
- Backup freshness — alert if no new backup file in 24h (see
  `docs/BACKUP_AND_RESTORE.md`).
