# GEO Tracker

**AI Visibility Intelligence platform.**

> See why AI recommends your competitors — and what to do about it.

GEO Tracker measures how often a brand appears in AI-generated answers,
explains why competitors outperform it, generates evidence-based
optimization opportunities, and verifies whether visibility changed after
an action was implemented.

**Core product loop:** `MEASURE → EXPLAIN → ACT → VERIFY`

---

## Status

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Repository and application foundation | IMPLEMENTED |
| 1 | Core database and multi-tenancy | IMPLEMENTED |
| 2 | Authentication, Workspaces and authorization | IMPLEMENTED |
| 3 | Entitlements, plans, usage and quotas | IMPLEMENTED |
| 4 | Project onboarding and prompt system | IMPLEMENTED |
| 5 | AI provider abstraction and integrations | IMPLEMENTED |
| 6 | Scan Engine | IMPLEMENTED |
| 7 | Brand / citation detection and metrics | IMPLEMENTED |
| 8 | Confidence Scans | IMPLEMENTED |
| 9 | Action Center / opportunity engine | IMPLEMENTED |
| 10 | Verification system | IMPLEMENTED |
| 11 | Scheduling and email reports | IMPLEMENTED |
| 12 | Dashboard and user interface | IMPLEMENTED |
| 13 | Agency dashboard and white-label reports | PLANNED |
| 14 | AppSumo licensing integration | PLANNED |
| 15 | Stripe billing | PLANNED |
| 16 | Admin, observability and operations | PLANNED |
| 17 | Security hardening and launch readiness | PLANNED |
| 18 | AppSumo launch preparation | PLANNED |

See `docs/` for detailed architecture and roadmap documentation.

---

## Features

- **Multi-tenant workspaces:** tenant boundary enforced on every
  workspace-scoped operation; cross-tenant access returns 404.
- **Authentication:** opaque server-side sessions with HttpOnly cookies,
  Argon2id password hashing, CSRF protection.
- **Entitlements:** plan-driven capabilities resolved from
  `BillingAccount` → `PlanDefinition` → `EffectiveEntitlements`.
  Fail-safe UNENTITLED behavior for misconfigured or lapsed workspaces.
  See `docs/ENTITLEMENTS.md`.
- **Usage and quotas:** atomic AI Check quota management with
  PostgreSQL row-level locking, idempotent reservations, and a monthly
  UTC quota period. AI usage is never unbounded. See
  `docs/USAGE_AND_QUOTAS.md`.
- **Project onboarding:** atomic project creation with brand/market
  configuration, keywords, competitors, and enabled LLM providers.
  Plan-based capacity limits enforced with row-level locking. See
  `docs/PROJECT_ONBOARDING.md`.
- **Stable versioned prompts:** deterministic prompt generation
  (5 variants per keyword: 3x NON_BRANDED, 1x BRANDED, 1x COMPETITOR)
  with versioned PromptSets that are never overwritten. Historical
  prompt sets are preserved for auditability. EN/PT language support.
  See `docs/PROMPT_SYSTEM.md`.
- **AI provider abstraction:** provider adapters for OpenAI, Anthropic,
  Google, and Perplexity with normalized request/result objects. Provider
  vs surface distinction (API results measure the surface, not the
  consumer UI). MODEL_ONLY and WEB_GROUNDED execution modes. No automatic
  retries, no quota calls, no hidden system prompts. Google WEB_GROUNDED
  disabled for compliance. httpx.AsyncClient with MockTransport tests.
  See `docs/PROVIDER_INTEGRATIONS.md` and `docs/PROVIDER_COMPLIANCE.md`.
- **STANDARD Scan Engine:** immutable PromptSet/PromptRun execution plans,
  full-plan quota reservation, duplicate-delivery row claims, bounded
  concurrency, no paid-call retries, stale recovery without provider replay,
  and durable response/source evidence. A successful PromptRun is exactly one
  customer AI Check even when provider web search performs internal calls;
  failed runs consume zero. See `docs/SCAN_ENGINE.md`.
- **Provider cost accounting:** exact append-only model/surface price rules,
  Decimal arithmetic, explicit `CostSource`, provider-reported Perplexity cost
  precedence, and null-as-unknown semantics. No production rules are seeded;
  operators must add verified rules for environment-configured model IDs. See
  `docs/COST_ACCOUNTING.md`.
- **Web application:** server-rendered HTML (Jinja2) with HTMX for
  progressive enhancement and Chart.js for trend visualization. Guided
  project onboarding wizard, workspace and project dashboards, Action
  Center with opportunity workflow, scan detail with polling, notification
  center, and schedule management. See `docs/WEB_APP.md`,
  `docs/ONBOARDING_UX.md`, and `docs/DASHBOARD_UX.md`.

---

## Technology stack

- **Backend:** Python 3.11, FastAPI, SQLAlchemy 2.x, Pydantic v2
- **Database:** PostgreSQL 15+
- **Cache / queue broker:** Redis
- **Background jobs:** Celery + Celery Beat
- **Migrations:** Alembic
- **Frontend:** Jinja2, HTMX 2.x, Tailwind CSS, Chart.js 4.x
- **Infrastructure:** Docker, Docker Compose, Nginx, HTTPS (production)
- **Quality:** pytest, Ruff, mypy (strict), GitHub Actions CI

### Python runtime baseline

The project targets **Python 3.11**. Docker, Ruff, and mypy are all
configured for Python 3.11. A `.python-version` file pins the runtime
for tools that support it (pyenv, uv, etc.).

---

## Local development

### Prerequisites

- Docker + Docker Compose (recommended), or
- Python 3.11 with local PostgreSQL 15+ and Redis 7+

### Using Docker Compose (recommended)

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec app alembic upgrade head
```

The API is available at `http://localhost:8000`.

### Using a local environment

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe (no external dependencies) |
| GET | `/ready` | Readiness probe (verifies PostgreSQL + Redis) |
| POST | `/api/v1/auth/register` | Create account + default workspace |
| POST | `/api/v1/auth/login` | Authenticate + issue session cookie |
| POST | `/api/v1/auth/logout` | Revoke session + clear cookie |
| GET | `/api/v1/auth/me` | Current user info + workspaces |
| GET | `/api/v1/auth/csrf` | Get CSRF token |
| GET | `/api/v1/workspaces` | List user's workspaces |
| POST | `/api/v1/workspaces` | Create workspace |
| GET | `/api/v1/workspaces/{id}` | Get workspace (member-only) |
| PATCH | `/api/v1/workspaces/{id}` | Update workspace (OWNER/ADMIN) |
| GET | `/api/v1/workspaces/{id}/entitlements` | Workspace entitlements (product capabilities) |
| GET | `/api/v1/workspaces/{id}/usage` | Monthly AI Check quota state |
| POST | `/api/v1/workspaces/{id}/projects/{project_id}/scans` | Create an idempotent STANDARD scan (ADMIN/OWNER) |
| GET | `/api/v1/workspaces/{id}/projects/{project_id}/scans` | List tenant-scoped scans |
| GET | `/api/v1/workspaces/{id}/projects/{project_id}/scans/{scan_id}` | Read scan and run evidence (cost fields hidden) |
| GET | `/docs` | OpenAPI interactive docs (non-production) |

---

## Validation commands

```bash
pytest                 # run all tests
ruff check .           # lint
ruff format --check .  # format check
mypy app               # static type checking
alembic upgrade head   # apply migrations
```

### Test database

Integration tests use a **dedicated test database** (`geo_tracker_test`),
never the development database (`geo_tracker`). The Docker Compose
PostgreSQL container auto-creates `geo_tracker_test` on first
initialization via `docker/postgres-init.sh`.

Integration tests prepare the schema via the **real Alembic migration
path** (`alembic upgrade head`), not `Base.metadata.create_all()`, so
migration drift is detectable.

Set the test database URL before running integration tests:

```bash
export DATABASE_URL="postgresql+psycopg://geo_tracker:geo_tracker_dev_password@localhost:15432/geo_tracker_test"
```

## CI

GitHub Actions runs the full validation suite on every push to `main`
and on pull requests targeting `main`:

- Ruff check + format check
- mypy (strict)
- Alembic migration to head (against a PostgreSQL 15 service)
- pytest (unit + integration)
- Alembic migration drift check (`alembic check`)

See `.github/workflows/ci.yml`.

---

## Project structure

```
geo-tracker/
├── app/
│   ├── main.py            # FastAPI entrypoint
│   ├── config.py          # environment-driven settings
│   ├── core/              # security, logging, exceptions, enums
│   ├── db/                # SQLAlchemy base, session, redis
│   ├── models/            # ORM models
│   ├── schemas/           # Pydantic schemas
│   ├── repositories/      # persistence layer
│   ├── services/          # domain / application services
│   ├── providers/         # AI provider adapters and registry
│   ├── routers/           # HTTP routers
│   ├── workers/           # Celery workers
│   ├── integrations/      # AppSumo / Stripe integrations
│   ├── admin/             # internal admin
│   ├── templates/         # Jinja2 templates
│   └── static/            # static assets
├── alembic/               # migrations
├── tests/                 # unit + integration tests
├── docs/                  # architecture documentation
├── scripts/               # operational scripts (seed, etc.)
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── alembic.ini
└── .env.example
```

---

## Security notes

- Passwords are hashed with Argon2id.
- Authentication uses opaque server-side sessions with HttpOnly cookies.
- CSRF protection on all state-changing requests.
- Session tokens are hashed (SHA-256) before storage in Redis.
- Secrets are loaded from environment variables; `.env` is git-ignored.
- Production secret validation: unsafe `APP_SECRET_KEY` values are rejected
  at startup in staging/production (see `docs/SECURITY.md`).
- Multi-tenant isolation enforced: cross-tenant access returns 404.
- See `docs/SECURITY.md` and `docs/AUTHENTICATION.md` for details.

---

## License

Proprietary. All rights reserved.
