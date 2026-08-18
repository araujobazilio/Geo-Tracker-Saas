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
- external providers (`app/providers/llm`)
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
