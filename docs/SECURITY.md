# Security

## Status

Phases 0–6 establish the implemented application, tenant, quota, provider, and
Scan Engine security boundaries described below. Phase 7 adds analysis
endpoint security. Phase 8 adds confidence scan tenant isolation. Additional
launch hardening is Phase 17.

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
