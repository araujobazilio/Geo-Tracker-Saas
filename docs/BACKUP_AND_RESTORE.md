# Backup and Restore

Backup and restore procedures for GEO Tracker. PostgreSQL holds all
authoritative business data and is the only datastore that must be
backed up. Redis holds ephemeral session and rate-limit state and is
never backed up.

## What lives where

### PostgreSQL — authoritative business data (MUST back up)

PostgreSQL is the system of record. It contains:

- **Identity & access:** `users`, `workspaces`, `workspace_members`,
  `audit_logs`.
- **Tenancy & projects:** `projects`, `project_keywords`, `competitors`,
  `project_providers`, `prompts`, `prompt_sets`.
- **Billing & entitlements:** `billing_accounts`, `appsumo_licenses`,
  `plan_definitions`, `plan_providers`, `workspace_usage_periods`.
- **Quota & usage accounting:** `quota_reservations`, `usage_events`
  (with database-level non-negative CHECK constraints).
- **Scan engine & evidence:** `scans`, `prompt_runs`, `response_sources`,
  `scan_entity_snapshots`, `scan_analyses`, `entity_mentions`,
  `source_attributions`.
- **Confidence & verification:** `opportunities`, `opportunity_occurrences`,
  `opportunity_evidence`, `opportunity_verifications`.
- **Scheduling & notifications:** `project_scan_schedules`,
  `notifications`, `notification_preferences`, `email_deliveries`.
- **Webhook ingest:** `provider_webhook_events`.
- **Pricing rules:** `provider_price_rules`.

Losing PostgreSQL means losing every workspace, scan, evidence row, and
quota accounting record. **Back it up on every release and on a daily
schedule.**

### Redis — ephemeral state (safe to lose)

Redis holds:

- **Server-side sessions:** opaque session tokens mapped to user data
  under a SHA-256 hash of the token (see `docs/AUTHENTICATION.md`).
  Losing Redis logs every user out — they must sign in again. No
  business data is lost.
- **Rate-limit counters:** login and register throttling windows. Losing
  Redis resets the counters — a minor, transient relaxation.

Redis runs with `--appendonly yes` in production for fast restart
recovery, but AOF is not a substitute for a PostgreSQL backup. **Redis
is never backed up and never restored.** If Redis is corrupted, stop the
container, delete the volume, and restart — users will be logged out
and rate limits reset, nothing more.

## Backup

### Backup command

The repository ships `scripts/backup_postgres.sh`, which produces a
timestamped `pg_dump` custom-format backup, verifies it is non-empty,
sets `0600` permissions, and optionally prunes old backups.

From the VPS, run it inside the `postgres` container so `pg_dump` is
available and the connection is local:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  /app/scripts/backup_postgres.sh /backups 30
```

If the script is not copied into the container, run `pg_dump` directly
and copy the file out:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  pg_dump -U "${DATABASE_USER}" -d "${DATABASE_NAME}" \
    --format=custom --no-owner --no-privileges \
    --file=/tmp/geo_tracker_$(date +%Y%m%d_%H%M%S).dump

# Copy the backup out of the container to the host.
docker compose -f docker-compose.prod.yml cp \
  postgres:/tmp/geo_tracker_<timestamp>.dump ./backups/
```

The script also accepts `DATABASE_URL` or `PG*` environment variables
(see its header comment for details).

### Verify the backup exists and is non-empty

The script already checks this and removes zero-byte/partial files, but
always confirm manually:

```bash
ls -lh ./backups/geo_tracker_*.dump
# The newest file must be non-zero.
test -s ./backups/geo_tracker_<timestamp>.dump && echo "OK" || echo "EMPTY/MISSING"
```

A custom-format `pg_dump` file is binary; verify it is a valid archive
with `pg_restore --list`:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  pg_restore --list /tmp/geo_tracker_<timestamp>.dump | head -20
```

A valid backup lists schema objects and data entries. An error here
means the backup is corrupt — take another one before proceeding.

### Ship backups off the VPS

Never keep the only copy of a backup on the same disk as the database.
Copy each backup to off-VPS storage (S3, B2, an off-site volume):

```bash
aws s3 cp ./backups/geo_tracker_<timestamp>.dump \
  s3://geo-tracker-backups/$(date +%Y/%m/%d)/geo_tracker_<timestamp>.dump \
  --sse AES256
```

Confirm the upload completed and the remote object size matches the
local file before deleting the local copy (if at all).

## Restore

### NEVER test restore over production

**Never run a restore against the production database to test it.** A
restore overwrites data. Always restore into a clean, separate test
database and validate there first. Only after validation is a
production restore considered, and only during a planned maintenance
window.

### Restore into a clean test database

1. Start a throwaway PostgreSQL container (or use a staging instance)
   with a fresh, empty database:

   ```bash
   docker run -d --name geo_restore_test -e POSTGRES_PASSWORD=test \
     -e POSTGRES_DB=geo_restore postgres:15-alpine
   ```

2. Copy the backup file into the test container:

   ```bash
   docker cp ./backups/geo_tracker_<timestamp>.dump geo_restore_test:/tmp/backup.dump
   ```

3. Restore with `pg_restore`:

   ```bash
   docker exec geo_restore_test \
     pg_restore -U postgres -d geo_restore --no-owner --no-privileges \
       --verbose /tmp/backup.dump
   ```

   `--no-owner` and `no-privileges` avoid failures from role mismatch
   between the production source and the test instance. Non-fatal
   warnings about roles are expected; any `FATAL` or `ERROR` halting
   the restore must be investigated.

4. Confirm the restore row counts look sane:

   ```bash
   docker exec geo_restore_test psql -U postgres -d geo_restore -c \
     "SELECT count(*) FROM workspaces;
      SELECT count(*) FROM scans;
      SELECT count(*) FROM quota_reservations;"
   ```

### Run migrations / check after restore

The restored schema is whatever was in the backup. Run Alembic to
confirm it is at a known revision and to apply any newer migrations the
current image expects:

```bash
docker exec geo_restore_test psql -U postgres -d geo_restore -c \
  "SELECT version_num FROM alembic_version;"
```

Then run the application's migration check against the test database
(using the test `DATABASE_URL`) to confirm the schema matches the image
you intend to validate:

```bash
DATABASE_URL=postgresql+psycopg://postgres:test@localhost:5432/geo_restore \
APP_ENV=test \
alembic upgrade head
```

A clean `upgrade head` (no-op or applied) confirms the restored backup
is compatible with the current code. If `upgrade head` fails, the
backup is from an incompatible schema lineage — investigate before any
production cutover.

### Application readiness validation

Point a throwaway app instance at the test database and confirm it
becomes ready:

```bash
docker run --rm -d --name geo_app_test \
  --link geo_restore_test:postgres \
  -e DATABASE_URL=postgresql+psycopg://postgres:test@postgres:5432/geo_restore \
  -e REDIS_URL=redis://localhost:6379/0 \
  -e APP_ENV=test \
  -p 18000:8000 \
  geo-tracker:<sha>

curl -fsS http://localhost:18000/ready
# expect: {"status":"ready","database":"ok","redis":"ok",...}
```

Then run a few read-only smoke checks against the restored data (log in
as a known user, list workspaces, open a scan). If everything reads
correctly, the backup is validated. Tear down the test containers:

```bash
docker rm -f geo_app_test geo_restore_test
```

### Production restore (planned maintenance only)

Only after a backup has been validated in a test database:

1. Announce a maintenance window. Take the app out of rotation (stop
   `app`, `worker`, `beat`).
2. Take a fresh "pre-restore" backup of the current production database
   in case the restore is wrong.
3. Drop/recreate the production database (or restore into a new volume
   and repoint `DATABASE_URL`).
4. `pg_restore` the validated backup.
5. Run `alembic upgrade head` via the `migrate` service.
6. Bring services back up and run the readiness + smoke checks from
   `docs/DEPLOYMENT.md`.

## Retention policy

| Backup type        | Frequency        | Retention | Storage |
|--------------------|------------------|-----------|---------|
| Pre-release        | Every release    | 90 days   | Off-VPS (S3/B2) |
| Daily scheduled    | Once per day     | 30 days   | Off-VPS (S3/B2) |
| Weekly             | Once per week    | 12 weeks  | Off-VPS (S3/B2) |

The `scripts/backup_postgres.sh` retention argument prunes local files
older than N days (`find ... -mtime +N -delete`). Off-VPS retention is
managed by bucket lifecycle rules — set them so old backups transition
to cheaper storage or expire automatically. Keep at least one backup
older than the most recent release so a rollback-to-before-release is
always possible.

Test a restore from off-VPS storage at least once a quarter. A backup
that has never been restored is an assumption, not a backup.
