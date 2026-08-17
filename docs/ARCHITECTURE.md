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
| Entitlements / quotas | PLANNED (Phase 3) |
| Project onboarding / prompts | PLANNED (Phase 4) |
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
