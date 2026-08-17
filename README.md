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
| 2 | Authentication, Workspaces and authorization | PLANNED |
| 3 | Entitlements, plans, usage and quotas | PLANNED |
| 4 | Project onboarding and prompt system | PLANNED |
| 5 | AI provider abstraction and integrations | PLANNED |
| 6 | Scan Engine | PLANNED |
| 7 | Brand / citation detection and metrics | PLANNED |
| 8 | Confidence Scans | PLANNED |
| 9 | Action Center / opportunity engine | PLANNED |
| 10 | Verification system | PLANNED |
| 11 | Scheduling and email reports | PLANNED |
| 12 | Dashboard and user interface | PLANNED |
| 13 | Agency dashboard and white-label reports | PLANNED |
| 14 | AppSumo licensing integration | PLANNED |
| 15 | Stripe billing | PLANNED |
| 16 | Admin, observability and operations | PLANNED |
| 17 | Security hardening and launch readiness | PLANNED |
| 18 | AppSumo launch preparation | PLANNED |

See `docs/` for detailed architecture and roadmap documentation.

---

## Technology stack

- **Backend:** Python 3.11, FastAPI, SQLAlchemy 2.x, Pydantic v2
- **Database:** PostgreSQL 15+
- **Cache / queue broker:** Redis
- **Background jobs:** Celery + Celery Beat
- **Migrations:** Alembic
- **Frontend:** Jinja2, HTMX, Tailwind CSS, Chart.js (planned)
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
│   ├── providers/llm/     # LLM provider abstraction
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
- Authentication uses HttpOnly cookies (planned for Phase 2).
- Secrets are loaded from environment variables; `.env` is git-ignored.
- Production secret validation: unsafe `APP_SECRET_KEY` values are rejected
  at startup in staging/production (see `docs/SECURITY.md`).
- Multi-tenant data model is in place; authenticated tenant-access
  enforcement is planned for Phase 2.
- See `docs/SECURITY.md` for details.

---

## License

Proprietary. All rights reserved.
