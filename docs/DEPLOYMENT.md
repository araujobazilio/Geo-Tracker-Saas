# Deployment

Production deployment guide for GEO Tracker on a single VPS using Docker
Compose. The production stack is defined in `docker-compose.prod.yml` and
consists of: `nginx`, `app`, `worker`, `beat`, `postgres`, `redis`, and a
one-shot `migrate` service.

## Prerequisites

### VPS sizing

| Resource | Minimum (closed beta) | Recommended |
|----------|------------------------|-------------|
| vCPU     | 2                      | 4           |
| RAM      | 4 GB                   | 8 GB        |
| Disk     | 40 GB SSD              | 80 GB SSD   |
| OS       | Ubuntu 22.04 LTS / Debian 12 | same   |

The closed-beta footprint is small: one `app` container (2 uvicorn
workers), one Celery worker (`concurrency=1`), one Celery beat
singleton, PostgreSQL 15, Redis 7, and Nginx. 4 GB RAM leaves headroom
for PostgreSQL `shared_buffers` and peak scan dispatch. Do not run under
2 GB — OOM kills during a scan burst are likely.

### Software on the VPS

- Docker Engine 24+ (with the `compose` v2 plugin).
- `git` to clone the repository.
- `certbot` (Snap or distro package) for Let's Encrypt TLS.
- `curl` for readiness/smoke checks.
- An S3-compatible bucket or off-VPS volume for backup shipping
  (optional but strongly recommended; see `BACKUP_AND_RESTORE.md`).

### Secrets and configuration

Create `.env.production` on the VPS (never commit it). At minimum:

```dotenv
APP_ENV=production
APP_SECRET_KEY=<64+ char random string>
ALLOWED_HOSTS=app.example.com
APP_PUBLIC_BASE_URL=https://app.example.com
DATABASE_USER=geo_tracker
DATABASE_PASSWORD=<strong unique password>
DATABASE_NAME=geo_tracker
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
CORS_ORIGINS=https://app.example.com
REGISTRATION_MODE=closed
EMAIL_ENABLED=true
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...
EMAIL_FROM_ADDRESS=noreply@app.example.com
# Provider keys (only those you are entitled to use)
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
```

The application fails fast at config load time if any production
invariant is violated (empty/placeholder `APP_SECRET_KEY`, missing
`ALLOWED_HOSTS`, non-HTTPS `APP_PUBLIC_BASE_URL`, dev database password
placeholder, `DEV_SEED_ENABLED=true`, or missing SMTP settings when
`EMAIL_ENABLED=true`). See `docs/SECURITY.md` Phase 13.

> **Note on `DATABASE_URL`**: Do not define `DATABASE_URL` in
> `.env.production`. The production Compose file constructs it from
> `DATABASE_USER`, `DATABASE_PASSWORD`, and `DATABASE_NAME`. Defining it
> in the env file with `${...}` references causes Docker Compose
> unresolved-variable interpolation warnings.

> **Note on healthchecks**: The Dockerfile and Compose app healthcheck
> derive the `Host` header from the first `ALLOWED_HOSTS` entry so the
> probe is authorized by `TrustedHostMiddleware` in production. The
> Nginx container has its own local `/nginx-health` liveness endpoint
> that does not proxy to FastAPI.

## Image tagging by Git SHA

Every release is built and tagged by the full Git SHA of the commit being
released:

```bash
git rev-parse HEAD            # e.g. 7f3c1a2b4d5e6f7...
docker build -t geo-tracker:7f3c1a2b4d5e6f7 .
```

`docker-compose.prod.yml` references `geo-tracker:${GIT_SHA:-latest}`.
Always export `GIT_SHA` before running compose commands so the stack
picks up the intended image:

```bash
export GIT_SHA=7f3c1a2b4d5e6f7
```

Never deploy `:latest` to production. The SHA tag is the immutable
rollback target and the value surfaced in `/health` and `/ready`
build metadata.

## Release sequence

Each release follows this exact order. Do not skip steps.

### 1. Pull / build the image by SHA

On the VPS, from the repository root:

```bash
git fetch --tags
git checkout 7f3c1a2b4d5e6f7
export GIT_SHA=7f3c1a2b4d5e6f7
docker compose -f docker-compose.prod.yml build app worker beat migrate
```

If you use a registry instead of building on the VPS:

```bash
docker pull ghcr.io/<org>/geo-tracker:${GIT_SHA}
docker tag  ghcr.io/<org>/geo-tracker:${GIT_SHA} geo-tracker:${GIT_SHA}
```

Confirm the image exists and is non-empty:

```bash
docker images geo-tracker:${GIT_SHA}
```

### 2. Back up the database

Always take a fresh backup before migrating. See
`docs/BACKUP_AND_RESTORE.md` for the full procedure. The short form:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  pg_dump -U "${DATABASE_USER}" -d "${DATABASE_NAME}" \
  --format=custom --no-owner --no-privileges \
  --file=/tmp/pre_release_${GIT_SHA}.dump

docker compose -f docker-compose.prod.yml cp postgres:/tmp/pre_release_${GIT_SHA}.dump \
  ./backups/pre_release_${GIT_SHA}.dump
```

Verify the backup file is non-empty (`ls -lh ./backups/pre_release_${GIT_SHA}.dump`).
If it is missing or zero bytes, **stop the release**.

### 3. Run the migration (separate step)

The `migrate` service is a one-shot container (`profiles: [migrate]`)
that runs `alembic upgrade head`. It is **not** started by `up -d`.
Run it explicitly so a migration failure halts the release before any
service is restarted:

```bash
docker compose -f docker-compose.prod.yml run --rm migrate
```

Inspect the output. A migration that errors must be treated as a release
blocker — do not proceed to step 4. Investigate, fix, and re-run.

### 4. Start / restart services

Bring up the long-running services with the new image:

```bash
docker compose -f docker-compose.prod.yml up -d nginx app worker beat postgres redis
```

Because the image tag changed, `up -d` recreates `app`, `worker`, and
`beat` from `geo-tracker:${GIT_SHA}`. `postgres` and `redis` are
recreated only if their configuration changed (data volumes persist).

### 5. Readiness check

Wait for the app healthcheck to pass, then probe readiness directly.
`/ready` verifies PostgreSQL and Redis connectivity, not just liveness:

```bash
docker compose -f docker-compose.prod.yml ps
curl -fsS https://app.example.com/ready
```

A `200` with `"status":"ready"` means the app can serve traffic. A
`503` means PostgreSQL or Redis is not reachable from the app — check
`docker compose logs app` and the `postgres`/`redis` healthchecks.

### 6. Smoke test

Run a small functional check against the live stack:

```bash
# Liveness + build metadata (confirms the deployed SHA).
curl -fsS https://app.example.com/health | grep "${GIT_SHA}"

# Readiness (DB + Redis).
curl -fsS https://app.example.com/ready

# Auth endpoint is reachable and rejects unauthenticated access.
curl -fsS -o /dev/null -w '%{http_code}\n' https://app.example.com/api/v1/auth/me
# expect 401

# Worker is alive.
docker compose -f docker-compose.prod.yml exec worker \
  celery -A app.workers.celery_app:celery_app inspect ping
```

If any smoke check fails unexpectedly, follow the rollback policy below.

## Rollback policy

Rollback is a deliberate, manual operation. There is **no automatic
rollback**.

### Rollback to the previous image tag

1. Identify the last known-good SHA (the previous tag in the registry or
   `docker images`).
2. Re-point the stack at it:

   ```bash
   export GIT_SHA=<previous-good-sha>
   docker compose -f docker-compose.prod.yml up -d nginx app worker beat
   ```
3. Re-run the readiness and smoke checks.

### Do NOT auto-alembic-downgrade

**Never run `alembic downgrade` automatically as part of a rollback.**
A downgrade can destroy data added by the new release and is not
reversible if a later migration depended on the new schema. Downgrades
are only ever performed by a human, against a known-clean restore, after
a written decision. The default rollback path is:

1. Roll back the application image to the previous SHA.
2. If the new migration added backward-incompatible schema that the old
   image cannot tolerate, **restore from the pre-release backup** (see
   below) instead of downgrading.

### Restore from backup if needed

If the new migration left the database in a state the previous image
cannot serve, restore the pre-release backup taken in step 2. Follow
`docs/BACKUP_AND_RESTORE.md` — restore into a clean database, validate,
then cut over. Never restore over the live production database without
first confirming the backup is valid in a test database.

## Nginx TLS setup with Let's Encrypt

Nginx terminates TLS and is the only service with published ports
(`80` and `443`). PostgreSQL and Redis have **no published ports** —
they are reachable only on the internal Docker network. The Nginx
configuration lives in `docker/nginx/nginx.conf` and is mounted
read-only into the container.

### One-time certbot runbook

Run this once per host. Replace `app.example.com` and the contact email.

```bash
# 1. Stop Nginx so certbot can bind to port 80.
docker compose -f docker-compose.prod.yml stop nginx

# 2. Issue the certificate using the standalone plugin.
sudo certbot certonly --standalone \
  -d app.example.com \
  --agree-tos --no-eff-email \
  --email ops@example.com

# 3. Create the TLS mount directory and link the certs.
sudo mkdir -p ./tls
sudo cp /etc/letsencrypt/live/app.example.com/fullchain.pem ./tls/fullchain.pem
sudo cp /etc/letsencrypt/live/app.example.com/privkey.pem   ./tls/privkey.pem
sudo chown -R "$(id -u):$(id -g)" ./tls
sudo chmod 600 ./tls/privkey.pem

# 4. Start Nginx.
docker compose -f docker-compose.prod.yml up -d nginx

# 5. Verify.
curl -fsS https://app.example.com/ready
```

### Renewal

Certbot certificates expire after 90 days. Add a weekly renewal cron
that copies renewed certs into `./tls` and reloads Nginx without
downtime:

```bash
# /etc/cron.weekly/geo-tracker-renew
#!/usr/bin/env bash
set -euo pipefail
DOMAIN="app.example.com"
certbot renew --quiet --deploy-hook "
  cp /etc/letsencrypt/live/${DOMAIN}/fullchain.pem ${REPO}/tls/fullchain.pem
  cp /etc/letsencrypt/live/${DOMAIN}/privkey.pem   ${REPO}/tls/privkey.pem
  chown -R \$(stat -c '%u:%g' ${REPO}/tls) ${REPO}/tls
  chmod 600 ${REPO}/tls/privkey.pem
  docker compose -f ${REPO}/docker-compose.prod.yml exec -T nginx nginx -s reload
"
```

Set `REPO` to the absolute repository path. Test the renewal flow with
`certbot renew --dry-run` before relying on it.

## Connection budget

Every long-running process opens a bounded pool of PostgreSQL
connections. The total must stay below PostgreSQL's `max_connections`
(minus reserved connections for maintenance/admin). The default
PostgreSQL `max_connections` is 100.

The budget is:

```
total = (web_workers × db_pool_size + web_workers × db_max_overflow)
      + (worker_concurrency × db_pool_size + worker_concurrency × db_max_overflow)
      + beat_connections
      + margin
```

With the closed-beta defaults from `app/config.py`
(`db_pool_size=5`, `db_max_overflow=10`) and the production compose
settings (`uvicorn --workers 2`, `celery worker --concurrency=1`, one
beat process):

| Component        | Count | Pool | Overflow | Max connections |
|------------------|-------|------|----------|-----------------|
| app (uvicorn)    | 2     | 5    | 10       | 30              |
| worker (celery)  | 1     | 5    | 10       | 15              |
| beat             | 1     | 5    | 10       | 15              |
| migrate (one-shot) | 1   | 5    | 10       | 15 (transient)  |
| **margin**       |       |      |          | **25**          |
| **PostgreSQL max_connections** | | | | **100** |

The steady-state ceiling is 30 + 15 + 15 = 60 connections, leaving a
25-connection margin for the one-shot `migrate` container, admin
sessions, and burst traffic. **If you raise `uvicorn --workers`,
`--concurrency`, `db_pool_size`, or `db_max_overflow`, recompute this
budget and raise PostgreSQL `max_connections` accordingly.** A safe rule
of thumb: keep the steady-state total at or below 70% of
`max_connections`.

Redis is single-threaded and does not have a connection budget in the
same sense, but each process holds a small pool. The default Redis
`maxclients` (10000) is far above anything this stack will open.
