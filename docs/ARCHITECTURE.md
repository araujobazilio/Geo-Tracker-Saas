# Architecture

## Overview

GEO Tracker is a **multi-tenant web SaaS** built as a **modular monolith**.
The product measures AI visibility (`MEASURE → EXPLAIN → ACT → VERIFY`).

```
Browser
   ↓
FastAPI Application
   ↓
Application / Domain Services
   ↓
PostgreSQL + Redis
   ↓
Celery Workers
   ↓
External AI APIs
```

## Architectural style

**Modular monolith.** Clear boundaries between:

- HTTP layer (`app/routers`)
- domain / application logic (`app/services`)
- persistence (`app/repositories`, `app/db`)
- external providers (`app/providers`)
- billing / entitlements (`app/services/entitlements`, `app/integrations`)
- background jobs (`app/workers`)
- integrations (`app/integrations`)

No microservices are introduced prematurely.

## Multi-tenancy

Tenant boundary = **Workspace**.

```
User → WorkspaceMembership → Workspace → Projects
```

Tenant-access enforcement is implemented: every protected workspace
operation validates membership via `WorkspaceAuthorizationService`.
Cross-tenant access returns 404. Public-facing identifiers are UUIDs
to reduce IDOR risk.

See `docs/MULTITENANCY.md`.

## Service layer

Domain and application logic lives in `app/services`. Each service is a
single responsibility boundary consumed by routers and other services.

| Service | Responsibility |
|---------|----------------|
| `WorkspaceAuthorizationService` | Tenant-access enforcement (membership checks) |
| `AuditService` | Centralized audit logging |
| `EntitlementService` | Resolves effective entitlements for a workspace |
| `QuotaService` | Atomic AI Check quota reservations and usage accounting |
| `ProjectOnboardingService` | Atomic project creation with keywords, competitors, providers, and initial prompt set |
| `ProjectService` | Project CRUD, status transitions (pause/activate/archive), summary |
| `KeywordService` | Keyword CRUD with normalization, capacity enforcement, revision tracking |
| `CompetitorService` | Competitor CRUD with domain normalization, capacity enforcement |
| `ProjectProviderService` | Provider configuration (PUT replace), entitlement enforcement |
| `PromptGenerationService` | Deterministic prompt generation (5 variants/keyword, EN/PT) |
| `PromptSetService` | PromptSet versioning, regeneration, staleness detection |

### Entitlement resolution

`EntitlementService` resolves what a workspace is entitled to via a
single chain:

```
BillingAccount (primary, eligible status) → plan_code → PlanDefinition → EffectiveEntitlements
```

It is **fail-safe**: if there is no primary billing account, the status
is not eligible, the plan code is missing/unknown, or the plan is
inactive, it returns a conservative `UNENTITLED` snapshot (all limits
zero, all flags false, no providers). It never raises. Routers and
services consume the immutable `EffectiveEntitlements` value object,
never billing tables directly.

See `docs/ENTITLEMENTS.md`.

### Quota reservation flow

`QuotaService` enforces AI Check quotas atomically using PostgreSQL
row-level locking (`SELECT ... FOR UPDATE`):

```
reserve_ai_checks()  →  ACTIVE reservation (quota held)
        ↓
provider call executes
        ↓
commit_ai_checks()   →  COMMITTED (used incremented, UsageEvent created)
        or
release_reservation() → RELEASED (unused reserved returned)
        or
expire_stale_reservations() → EXPIRED (TTL passed)
```

PostgreSQL is the quota source of truth (NOT Redis). The monthly quota
period is the UTC calendar month. Reservations are idempotent via
`idempotency_key` and retained after completion for history.

See `docs/USAGE_AND_QUOTAS.md`.

## Provider abstraction (Phase 5/5.1)

AI provider adapters live in `app/providers`. They translate
`ProviderRequest` into provider-specific HTTP calls and normalize
responses into `ProviderResult`.

```
ProviderRequest → ProviderAdapter.execute() → ProviderResult
```

Key design decisions:

- **Provider vs surface**: `LLMProvider` identifies the company;
  `ProviderSurface` identifies the specific API endpoint. API results
  measure the surface, not the consumer UI.
- **Execution modes**: `MODEL_ONLY` (no web search) and `WEB_GROUNDED`
  (web search tool). Not all providers support all modes.
- **WEB_GROUNDED integrity**: WEB_GROUNDED success ALWAYS implies
  `search_used == True`. If no search is observed, `ProviderSearchError`
  is raised. This is a critical methodological invariant.
- **No automatic retries**: One `execute()` = at most ONE billable
  request. Scan Engine (Phase 6) owns retry policy.
- **No quota/usage in adapters**: Adapters do NOT call `QuotaService`
  or create `UsageEvent`. Scan Engine owns accounting.
- **No hidden system prompts**: The prompt text is the experimental
  input. Adapters add only the minimum API envelope.
- **Request ID vs response ID**: `provider_request_id` is the HTTP
  request/support identifier (e.g. `x-request-id` header);
  `provider_response_id` is the generated resource/object identifier
  (e.g. response ID, message ID, interaction ID). These are distinct.
- **Provider-reported cost**: `provider_reported_cost_usd` (Decimal)
  preserves the cost reported by the provider (e.g. Perplexity), not
  our own pricing calculation.
- **Malformed JSON normalization**: All adapters normalize invalid JSON
  to `ProviderResponseError`. Parser exceptions never leak.
- **httpx.AsyncClient**: Unified transport for all providers;
  `MockTransport` for deterministic tests.
- **Lazy registry**: `ProviderRegistry` constructs adapters on demand.
  Missing credentials do NOT crash application startup.
  `capabilities()` works WITHOUT credentials (static adapter facts).
- **Google Interactions API**: The Google adapter uses the current
  recommended Interactions API (`POST /v1beta/interactions`), not the
  legacy `generateContent`. `store=false` for stateless one-shot
  measurements. Thought/reasoning steps are discarded.

See `docs/PROVIDER_INTEGRATIONS.md` and `docs/PROVIDER_COMPLIANCE.md`.

## Technology stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI |
| ORM | SQLAlchemy 2.x |
| Validation | Pydantic v2 |
| Database | PostgreSQL 15+ |
| Migrations | Alembic |
| Cache / broker | Redis |
| Background jobs | Celery + Celery Beat |
| Frontend | Jinja2, HTMX, Tailwind CSS, Chart.js (planned) |
| Auth | Opaque server-side sessions, HttpOnly cookies, Argon2id |
| Infra | Docker, Docker Compose, Nginx, HTTPS (production) |
| Quality | pytest, Ruff, mypy (strict) |

## Configuration

All configuration is environment-driven via `pydantic-settings`
(see `app/config.py` and `.env.example`). No secrets are hardcoded.

## Logging

Structured logging via `structlog`. JSON output in production,
human-readable in development. Logs never contain secrets.

## Health / readiness

- `GET /health` — liveness, no external dependencies.
- `GET /ready` — readiness, verifies PostgreSQL + Redis.

These endpoints live outside `/api/v1` versioning.

## API versioning

Application APIs are namespaced under `/api/v1/` (added in later phases).
Infrastructure endpoints (`/health`, `/ready`) are unversioned.

## Roadmap status

| Feature | Status |
|---------|--------|
| Application foundation | IMPLEMENTED (Phase 0) |
| Core multi-tenant data model | IMPLEMENTED (Phase 1) |
| Authentication, workspaces, authorization | IMPLEMENTED (Phase 2) |
| Entitlements / quotas | IMPLEMENTED (Phase 3) |
| Project onboarding / prompts | IMPLEMENTED (Phase 4) |
| AI provider abstraction | PLANNED (Phase 5) |
| Scan Engine | PLANNED (Phase 6) |
| Brand / citation detection | PLANNED (Phase 7) |
| Confidence Scans | PLANNED (Phase 8) |
| Action Center | PLANNED (Phase 9) |
| Verification system | PLANNED (Phase 10) |
| Scheduling / email reports | PLANNED (Phase 11) |
| Dashboard / UI | PLANNED (Phase 12) |
| Agency dashboard / white-label | PLANNED (Phase 13) |
| AppSumo licensing | PLANNED (Phase 14) |
| Stripe billing | PLANNED (Phase 15) |
| Admin / observability | PLANNED (Phase 16) |
| Security hardening | PLANNED (Phase 17) |
| AppSumo launch prep | PLANNED (Phase 18) |
