# Security

## Status

Phases 0–6 establish the implemented application, tenant, quota, provider, and
Scan Engine security boundaries described below. Phase 7 adds analysis
endpoint security. Phase 8 adds confidence scan tenant isolation. Phase 9
adds Action Center and Competitor Explanation zero-cost endpoint security.
Phase 10 adds Verification Scan tenant isolation, role enforcement, and
VERIFIED status protection. Additional launch hardening is Phase 17.

## Implemented

- **Password hashing:** Argon2id via `argon2-cffi` (`app/core/security.py`).
- **Opaque server-side sessions:** session tokens are cryptographically
  secure random values; session data stored in Redis under SHA-256 hash
  of the token. See `docs/AUTHENTICATION.md`.
- **Cookie security:** HttpOnly, SameSite=Lax, Secure (enforced in
  staging/production), Path=/, explicit Max-Age.
- **CSRF protection:** server-side CSRF token stored in session; validated
  via `X-CSRF-Token` header on POST/PUT/PATCH/DELETE. Constant-time
  comparison.
- **Session fixation protection:** new token issued on every login.
- **Login timing mitigation:** dummy Argon2id verification for nonexistent
  users.
- **Password rehash:** outdated Argon2id parameters detected and upgraded
  on successful login.
- **Rate limiting:** Redis-backed throttling for login and register
  endpoints.
- **Email normalization:** canonical lowercase storage prevents
  casing-based duplicate accounts.
- **Secrets:** loaded from environment via `pydantic-settings`; `.env` is
  git-ignored; only `.env.example` is committed.
- **Production secret validation:** `APP_SECRET_KEY` is validated at config
  load time. In staging/production, empty, known-placeholder, or
  too-short (< 32 chars) secrets are rejected with a clear error. The
  real secret value is never included in error messages.
  See `app/config.py` (`Settings._validate_production_secret`).
- **Constant-time comparison:** `safe_eq` helper for sensitive comparisons.
- **Tenant isolation enforcement:** `WorkspaceAuthorizationService` checks
  database membership for every workspace-scoped operation. Non-members
  receive 404 (not 403) to avoid revealing resource existence.
  See `docs/MULTITENANCY.md`.
- **UUIDs:** public-facing identifiers are UUIDs to reduce IDOR risk.
- **Database accounting integrity:** `usage_events` has database-level
  non-negative CHECK constraints for `ai_checks`, token counts, and
  `cost_usd` (see `docs/DATABASE.md`).
- **AppSumo license uniqueness:** `external_license_id` is UNIQUE at the
  database level.
- **Quota enforcement (cost protection):** AI usage is NEVER unbounded.
  `monthly_ai_checks` is always a finite integer on every plan (never
  unlimited) to protect paid-provider API economics. `QuotaService`
  reserves quota before every AI call using PostgreSQL row-level locking
  (`SELECT ... FOR UPDATE`) to prevent concurrent oversubscription.
  See `docs/USAGE_AND_QUOTAS.md`.
- **Fail-safe UNENTITLED behavior:** `EntitlementService` never raises.
  If a workspace has no primary billing account, an ineligible status,
  a missing/unknown plan code, or an inactive plan, it returns a
  conservative `UNENTITLED` snapshot (all limits zero, all flags false,
  no providers). A misconfigured or lapsed workspace cannot accidentally
  access paid capabilities. See `docs/ENTITLEMENTS.md`.
- **Usage accounting idempotency:** `usage_events.idempotency_key` is
  unique when present, preventing double-counted AI Checks, tokens, or
  cost on provider-call retries. `quota_reservations.idempotency_key`
  is unique, making reservation retries idempotent.
- **Entitlements/usage endpoint isolation:** the
  `GET /api/v1/workspaces/{workspace_id}/entitlements` and
  `GET /api/v1/workspaces/{workspace_id}/usage` endpoints require
  authentication and workspace membership via
  `WorkspaceAuthorizationService`. Cross-tenant access returns 404
- **Project resource isolation (Phase 4):** all project, keyword,
  competitor, provider, and prompt-set endpoints require workspace
  membership. Write operations (create, update, pause, archive,
  regenerate) require ADMIN or OWNER role. Cross-workspace access to
  project resources returns 404. Keyword and competitor IDs are scoped
  to their project — accessing a keyword from project B via project A
  returns 404/409. See `docs/PROJECT_ONBOARDING.md`.
- **Plan-based capacity enforcement (Phase 4):** project, keyword, and
  competitor limits are enforced with PostgreSQL row-level locking
  (`SELECT ... FOR UPDATE`) to prevent concurrent oversubscription.
  Archived projects free capacity but reactivation re-checks limits.
- **Prompt stability (Phase 4):** prompt sets are versioned and never
  overwritten. Historical prompt sets are preserved with `ON DELETE
  RESTRICT` on the `prompts → prompt_sets` foreign key, ensuring audit
  history cannot be silently destroyed. See `docs/PROMPT_SYSTEM.md`.
- **Provider API key security (Phase 5):** provider API keys are
  `SecretStr` in `Settings`, loaded from server environment only. They
  are NEVER persisted in the database, sent to the browser, or exposed
  in `repr(adapter)`, structured logs, or sanitized exceptions.
  Authorization headers are never logged. Raw provider response bodies
  are not dumped into error messages. BYOK is deferred. No provider-key
  API endpoints exist. See `docs/PROVIDER_INTEGRATIONS.md`.
- **Provider compliance boundary (Phase 5):** Google Search grounding
  is disabled in the Gemini adapter due to terms that conflict with
  automated storage/analysis. `WEB_GROUNDED` requests fail with
  `ProviderModeNotAllowedError` BEFORE any network call. The Google
  adapter measures `GOOGLE_INTERACTIONS_API` MODEL_ONLY, NOT Google AI
  Overviews. See `docs/PROVIDER_COMPLIANCE.md`.
- **No public provider execution endpoint (Phase 5):** there is no
  `POST /api/provider/execute` or similar endpoint that allows
  arbitrary provider credit spending. Provider adapters are internal
  only. Phase 6 Scan Engine owns user-triggered execution under quota.
- **Economically safe Scan execution (Phase 6):** each adapter performs at
  most one billable request and the Scan Engine does not retry it. Celery uses
  early acknowledgement (`task_acks_late=False`); the task has no autoretry and
  never calls `self.retry`. PostgreSQL row claims make duplicate delivery a
  no-op. Stale recovery marks unresolved work failed and never repeats provider
  calls, so worker-loss ambiguity is absorbed by GEO rather than risked as a
  duplicate customer charge. See `docs/SCAN_ENGINE.md`.
  (not 403) to avoid revealing resource existence, consistent with all
  other workspace-scoped endpoints. These endpoints expose only product
  capabilities and quota state — never billing internals (customer IDs,
  license IDs).
- **Phase 6 evidence/accounting boundary:** the full STANDARD plan is reserved
  before dispatch. A successful PromptRun atomically stores evidence and commits
  exactly one AI Check; failures roll back and consume zero, then unused quota is
  released. Customer Scan schemas expose final evidence and provider-returned
  sources but hide tokens, costs, CostSource, pricing rules, usage-event IDs,
  and reservation IDs. Source URLs are retained without server-side fetching,
  avoiding Phase 6 SSRF exposure.
- **Exact internal pricing:** cost uses Decimal and append-only exact
  provider/surface/model/effective-time rules; unknown cost remains NULL rather
  than a misleading zero. No production prices are seeded for environment-driven
  model IDs. See `docs/COST_ACCOUNTING.md`.
- **Plan limit integrity:** `plan_definitions` limits are typed columns
  with database-level CHECK constraints (non-negative), not JSON blobs.
  `quota_reservations` enforces `reserved > 0` and
  `committed <= reserved` at the database level.
- **Audit logging:** centralized `AuditService` records auth and workspace
  events. Never stores session tokens, CSRF tokens, passwords, or hashes.
- **Logging:** structured logs never include passwords, API keys, or tokens.
- **Error handling:** application errors never expose stack traces to
  clients (see `app/core/exceptions.py`).

## Phase 7: Analysis endpoint security

Phase 7 adds deterministic analysis and visibility-metrics endpoints scoped to
a scan resource. The endpoints are:

- `POST /api/v1/workspaces/{wid}/projects/{pid}/scans/{sid}/analysis` —
  ADMIN only (run or retry analysis).
- `GET /api/v1/workspaces/{wid}/projects/{pid}/scans/{sid}/analysis` —
  MEMBER+ (view analysis).
- `GET /api/v1/workspaces/{wid}/projects/{pid}/scans/{sid}/metrics` —
  MEMBER+ (view metrics).
- `GET /api/v1/workspaces/{wid}/projects/{pid}/scans/{sid}/runs/{rid}/analysis`
  — MEMBER+ (view per-run evidence).

Security properties:

- **Tenant isolation:** all four endpoints enforce workspace membership via
  `WorkspaceAuthorizationService`. Cross-tenant access returns 404 (not 403)
  to avoid revealing resource existence, consistent with all other
  workspace-scoped endpoints.
- **Role matrix:** write operations (`POST`) require ADMIN; read operations
  (`GET`) require MEMBER or above.
- **No cost-injection vector:** analysis consumes 0 AI Checks, 0 provider
  calls, and 0 `UsageEvents`. There is no endpoint that lets a user trigger
  billable provider spending via analysis.
- **No user-controlled LLM input:** analysis operates only on persisted
  evidence. No user-controlled input is sent to any LLM provider.
- **POST idempotency:** re-running `POST .../analysis` returns the existing
  `COMPLETED` analysis without duplicating evidence.
- **Safe URL host parsing:** host extraction uses only stdlib
  `urllib.parse` — no DNS resolution, no HTTP requests, no redirect
  following.
- **Safe mention detection:** mention detection uses only stdlib `re` and
  `unicodedata` — no external services are contacted.
- **Evidence immutability:** `ScanEntitySnapshots` are immutable. Users
  cannot modify entity terms after scan creation to influence analysis
  results.

## Phase 8: Confidence scan security

Phase 8 adds CONFIDENCE scan endpoints that create repeated-observation
scans from a baseline STANDARD scan and retrieve reliability metrics.

Security properties:

- **Baseline scan tenant isolation:** a CONFIDENCE scan can only be
  created from a baseline scan that belongs to the same workspace.
  Workspace A cannot use Workspace B's scan as a baseline — the
  baseline lookup is workspace-scoped and returns 404 (not 403) for
  cross-tenant access, consistent with all other workspace-scoped
  resources.
- **Confidence results tenant isolation:** confidence scan results and
  reliability metrics are scoped to the owning workspace. Workspace A
  cannot read Workspace B's confidence results — cross-tenant access
  returns 404 (not 403), consistent with all other workspace-scoped
  endpoints.
- **Role matrix:**

  | Role | Create CONFIDENCE scan | Read confidence results |
  |------|------------------------|-------------------------|
  | OWNER | Yes | Yes |
  | ADMIN | Yes | Yes |
  | MEMBER | No | Yes |

  Write operations (create CONFIDENCE scan) require ADMIN or OWNER.
  Read operations (retrieve confidence results and metrics) require
  MEMBER or above.
- **Entitlement gating:** CONFIDENCE scan creation requires the
  `confidence_scans` entitlement flag. A workspace without this
  entitlement cannot create CONFIDENCE scans, regardless of role.
- **No cost-injection vector beyond STANDARD:** CONFIDENCE scans use
  the same full-reservation-before-dispatch, no-retry, atomic-commit
  model as STANDARD scans. Failed observations release unused quota at
  finalization. There is no endpoint that lets a user trigger
  uncontrolled provider spending.

## Phase 9: Action Center and Competitor Explanation security

Phase 9 adds Action Center and Competitor Explanation endpoints that
read persisted Scan evidence and generate deterministic Opportunities.

Security properties:

- **Tenant isolation:** all endpoints enforce workspace membership via
  `WorkspaceAuthorizationService`. Cross-tenant access returns 404 (not
  403) to avoid revealing resource existence, consistent with all other
  workspace-scoped endpoints.
- **Role matrix:**

  | Role | Read explanations/opportunities | Refresh actions | Update status |
  |------|----------------------------------|-----------------|---------------|
  | OWNER | Yes | Yes | Yes |
  | ADMIN | Yes | Yes | Yes |
  | MEMBER | Yes | No | No |

  Read operations (list/get competitor explanations, list/get
  opportunities) require MEMBER or above. Write operations (refresh
  actions, update opportunity status) require ADMIN or OWNER.
- **Zero-cost endpoints:** Action refresh and competitor explanation
  endpoints perform only deterministic local computation. They consume
  zero AI Checks, zero UsageEvents, and zero paid provider calls. There
  is no cost-injection vector — a user cannot trigger provider spending
  via these endpoints.
- **Analysis readiness (fail closed):** both competitor explanation and
  action generation require a `COMPLETED` `ScanAnalysis`. A missing,
  `PENDING`, `RUNNING`, or `FAILED` analysis raises `ConflictError`,
  not zero visibility. This prevents users from interpreting absent
  evidence as a measured zero.
- **Historical snapshot integrity:** competitor explanations use
  immutable `ScanEntitySnapshot` records, not current mutable
  `Project`/`Competitor` state. A user cannot rename a brand or
  competitor after scan creation to influence explanation results.
- **VERIFIED status protection:** the `VERIFIED` opportunity status is
  read-only. No API endpoint can manually transition an Opportunity to
  `VERIFIED`. Only the system-only
  `OpportunityWorkflowService.mark_verified_from_verification()` method
  can set it, and only when a Verification evaluation produces a
  `RESOLVED` outcome. See Phase 10 below.
- **Cross-project isolation:** accessing an Opportunity from a different
  project (within the same workspace) returns 404. Opportunity queries
  are scoped to `(workspace_id, project_id)`.

## Phase 10 + 10.1: Verification Scan security

Phase 10 adds Verification Scan endpoints that create VERIFICATION
scans, list verification records, and trigger deterministic
evaluation. Phase 10.1 hardens the scope, outcome integrity, and
automatic evaluation.

Security properties:

- **Verification scan tenant isolation:** a VERIFICATION scan can only
  be created from an Opportunity that belongs to the caller's
  workspace. The baseline scan, baseline occurrence, and parent
  Opportunity must all belong to the same workspace. Cross-tenant
  access returns 404.
- **Verification record tenant isolation:** verification records are
  scoped to `(workspace_id, project_id, opportunity_id)`. Cross-tenant
  access returns 404.
- **Role enforcement:** creating a verification scan and triggering
  evaluation require `OWNER`/`ADMIN` role. Listing and reading
  verification records require `MEMBER` role (read-only).
- **Entitlement enforcement:** verification scan creation requires the
  `verification_scans` entitlement flag on the workspace's effective
  plan. A workspace without this entitlement cannot create verification
  scans; the endpoint returns a 403/entitlement error before any quota
  is reserved.
- **Methodology cloning integrity:** the VERIFICATION scan clones the
  frozen baseline's exact prompts, providers, surfaces, execution
  modes, requested models, and entity snapshots. Current project
  configuration cannot alter the cloned methodology. This prevents a
  user from weakening the verification measurement by changing project
  settings after implementation.
- **VERIFIED status is system-only:** no API endpoint can manually
  transition an Opportunity to `VERIFIED`. Only
  `mark_verified_from_verification()` can set it, and only when the
  evaluation produces `RESOLVED`. `VERIFIED` is read-only — no
  transitions away from it are allowed.
- **Targeted scope integrity (Phase 10.1):** the
  `VerificationScopeResolver` selects the exact historical baseline
  cells per OpportunityType. The same scope drives BOTH the provider
  execution plan AND the baseline/verification evaluation, ensuring
  that the comparison is performed on corresponding methodological
  cells. This prevents a user from altering the scope by changing
  project settings after implementation.
- **One pending verification per cycle (Phase 10.1):** at most one
  PENDING verification may exist per implementation cycle. Enforced at
  the service level AND the database level (partial unique index on
  `opportunity_verifications(opportunity_id, baseline_occurrence_id)
  WHERE outcome = 'PENDING'`).
- **Idempotency key conflict detection (Phase 10.1):** reusing the
  same idempotency key after a re-implementation cycle (different
  baseline occurrence) raises `ConflictError`, preventing accidental
  cross-cycle idempotency collisions.
- **Automatic evaluation safety (Phase 10.1):** automatic evaluation
  after ScanAnalysis failure is logged and swallowed — it MUST NOT
  rollback the analysis, replay providers, or change quota. The
  verification remains PENDING and can be evaluated manually.
- **Concurrency safety:** `OpportunityWorkflowService.transition()`
  acquires a row-level lock on the Opportunity.
  `VerificationEvaluationService.evaluate()` acquires a row-level lock
  on the `OpportunityVerification` record.
  `mark_verified_from_verification()` re-checks the Opportunity status
  under lock before transitioning, preventing race conditions where
  the user changes the status while the verification is in flight.
- **Zero-cost evaluation:** `VerificationEvaluationService.evaluate()`
  creates zero UsageEvents, consumes zero AI Checks, and makes zero
  provider calls. Only the scan execution costs AI Checks.

## Planned (later phases)

- Rate limiting (Phase 17).
- Webhook signature verification + replay protection (Phase 14/15).
- Encrypted secret storage for future BYOK (Phase 17+).
- Output escaping in templates (Phase 12).

## Rules

- Never store authentication credentials or bearer tokens in `localStorage`.
- Never expose server API keys to frontend JavaScript.
- Never commit `.env`, credentials, private keys, or access tokens.
- Never log secrets, credentials, or authentication tokens.
- Never store passwords, API secrets, or auth tokens in audit logs.

## Phase 13: Production hardening

Phase 13 hardens the deployment surface for single-VPS production with
Docker Compose. The properties below are enforced by the production
Dockerfile, the application middleware stack, configuration validation,
and the `docker-compose.prod.yml` network topology. See
`docs/DEPLOYMENT.md`, `docs/OPERATIONS.md`, and `docs/BACKUP_AND_RESTORE.md`
for operational procedures.

### Production Dockerfile

- **Multi-stage build:** `Dockerfile` uses three stages —
  `frontend-build` (Node 24, builds Tailwind CSS), `python-builder`
  (Python 3.11, installs production dependencies into a target
  directory), and `runtime` (Python 3.11-slim, copies only the built
  packages and application source). No compiler toolchain is present
  in the runtime image.
- **Production dependencies only:** the builder runs
  `pip install --target=/install .` (NOT `.[dev]`), so test/lint/dev
  tooling is absent from the runtime image. `uvicorn[standard]` is
  installed explicitly for production serving.
- **Non-root runtime user:** the runtime stage creates a system user
  `geo` (uid/gid 1001) with no home directory and runs as `USER geo`.
  The application never runs as root.
- **Minimal runtime deps:** only `libpq5`, `libffi8`, and `curl` (for
  the healthcheck) are installed at runtime. `apt` lists are cleaned.
- **No dev reload:** the production command is
  `uvicorn ... --host 0.0.0.0 --port 8000 --workers 2` with no
  `--reload`. The healthcheck uses `curl -fsS /health`.

### ALLOWED_HOSTS / trusted host validation

- **`TrustedHostMiddleware`** (`app/middleware/trusted_host.py`) rejects
  requests whose `Host` header (port stripped) is not in
  `settings.allowed_host_list` with a `400` response. This prevents
  Host header spoofing attacks (cache poisoning, password-reset link
  manipulation, routing bypass).
- **`ALLOWED_HOSTS` is required in production:** `Settings` fail-fast
  validation rejects an empty `ALLOWED_HOSTS` when `APP_ENV=production`
  (see `app/config.py`, `_validate_production_secret`). The application
  will not start without it.
- In development/test, `ALLOWED_HOSTS` is empty and all hosts are
  allowed (preserving `localhost` ergonomics).

### Security headers

`SecurityHeadersMiddleware` (`app/middleware/security_headers.py`) is
added early in the middleware stack so headers are present on all
responses, including error responses:

- **`X-Content-Type-Options: nosniff`** — always.
- **`Referrer-Policy: strict-origin-when-cross-origin`** — always.
- **`X-Frame-Options: DENY`** — always (clickjacking protection).
- **`Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()`**
  — always.
- **`Content-Security-Policy`** — `default-src 'self'`,
  `script-src 'self' 'unsafe-inline'`, `style-src 'self' 'unsafe-inline'`,
  `img-src 'self' data:`, `frame-ancestors 'none'`, `base-uri 'self'`,
  `form-action 'self'`. Inline scripts/styles are allowed only because
  the current Jinja/HTMX/Chart.js UI uses small inline blocks; a future
  refactor to static JS files will tighten this to remove
  `'unsafe-inline'`.
- **`Strict-Transport-Security`** — `max-age=31536000;
  includeSubDomains; preload`, sent **only** in staging/production
  (`settings.is_staging or settings.is_production`) to avoid breaking
  local HTTP development.

Nginx (`docker/nginx/nginx.conf`) supplements the app middleware with
the same headers via `add_header ... always`, so headers are present
even on responses Nginx generates directly (e.g. the HTTP→HTTPS
`301`).

### Request correlation ID

`RequestCorrelationMiddleware` (`app/middleware/correlation.py`):

- Accepts an incoming `X-Request-ID` only if it matches
  `^[A-Za-z0-9_-]{1,64}$` — this prevents log injection via newline or
  control characters. Invalid or missing IDs are replaced with a fresh
  UUID4 hex.
- Binds the correlation ID into `structlog` contextvars so every
  structured log line for the request includes `request_id`.
- Returns the correlation ID as the `X-Request-ID` response header,
  enabling end-to-end tracing from Nginx through the app to the worker.
  Nginx forwards `$request_id` as `X-Request-ID` on proxied requests.

No secrets, tokens, or credentials are ever included in the
correlation ID.

### Closed beta registration gate

- **`REGISTRATION_MODE`** (`app/config.py`) is a `Literal["open",
  "closed", "invite_only"]` defaulting to `"open"` for development.
  `Settings.is_registration_closed` returns `True` for `"closed"` and
  `"invite_only"`.
- For closed beta, set `REGISTRATION_MODE=closed` in
  `.env.production`. The register endpoint refuses new sign-ups while
  login for existing users continues to work. This prevents
  uncontrolled account creation during the beta period.
- Production validation does not force a specific mode (an operator may
  legitimately choose `invite_only`), but the default `open` should be
  set deliberately, not accidentally.

### Production config fail-fast validation

`Settings._validate_production_secret` (`app/config.py`) runs at config
load time and raises immediately — the application never starts — if any
production invariant is violated:

- `APP_SECRET_KEY` empty, a known placeholder (`change-me`,
  `change-me-to-a-long-random-string`), or shorter than 32 characters.
  The real secret value is never included in the error message.
- `DATABASE_URL` contains the development password placeholder
  `geo_tracker_dev_password`.
- `REDIS_URL` is empty.
- `APP_PUBLIC_BASE_URL` does not start with `https://`.
- `ALLOWED_HOSTS` is empty (production only).
- `DEV_SEED_ENABLED` is `true` (production only).
- `EMAIL_ENABLED=true` without `SMTP_HOST` or `EMAIL_FROM_ADDRESS`.
- `SESSION_COOKIE_SECURE` is not set — it is forced to `True` in
  staging/production.
- Confidence scan repeat-count bounds (`default >= 2`, `max >= default`,
  `max <= 10`).

This means a misconfigured production deploy fails loudly at boot
rather than serving insecure traffic.

### No public DB / Redis ports

In `docker-compose.prod.yml`, `postgres` and `redis` publish **no
ports** — they are reachable only on the internal Docker network. Only
`nginx` publishes ports (`80` and `443`). The `app`, `worker`, and
`beat` services use `expose` (internal only) or no port mapping at all.
This means:

- PostgreSQL is not reachable from the public internet — no remote
  brute-force or credential-stuffing surface.
- Redis is not reachable from the public internet — no unauthenticated
  Redis exposure.
- All inter-service communication (app→postgres, app→redis,
  worker→postgres, worker→redis, beat→redis) stays on the Docker
  bridge network.

### TLS via Nginx

- Nginx terminates TLS (`listen 443 ssl`) with certificates mounted
  read-only from `./tls` (`docker/nginx/nginx.conf`). TLS 1.2 and 1.3
  only; `ssl_ciphers HIGH:!aNULL:!MD5`; server cipher preference enabled.
- Port 80 returns a `301` to HTTPS for all requests.
- Nginx forwards `X-Forwarded-For` (overwritten with the real client IP,
  not appended to an untrusted chain), `X-Forwarded-Proto`, `Host`, and
  `X-Real-IP` to the app. The app trusts these headers because it is
  never exposed directly — Nginx is the only ingress.
- Certificates are provisioned and renewed with Let's Encrypt
  `certbot`; see the certbot runbook in `docs/DEPLOYMENT.md`.

### Cookie Secure flag enforced

- `SESSION_COOKIE_SECURE` is forced to `True` in staging/production by
  `Settings._validate_production_secret`. Session cookies are only
  ever sent over HTTPS.
- All session cookies are `HttpOnly`, `SameSite=Lax`, `Path=/`, with an
  explicit `Max-Age` (see the existing Cookie security property above).
  The `Secure` flag is not optional in production — it is enforced at
  config load time, so a misconfiguration cannot ship a cookie over
  HTTP.
