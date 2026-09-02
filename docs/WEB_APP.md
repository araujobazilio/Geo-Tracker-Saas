# GEO Tracker Web Application

The web application provides a browser-based UI for the GEO Tracker platform,
complementing the REST API. It is built with server-rendered HTML templates
(Jinja2), HTMX for progressive enhancement, and Chart.js for trend
visualization.

## Architecture

### Stack

- **Server**: FastAPI with synchronous route handlers (the ORM is synchronous)
- **Templates**: Jinja2Templates (Starlette) — `app/templates/`
- **Static assets**: `app/static/` — CSS, JS, vendor libraries
- **HTMX 2.x**: Progressive enhancement for polling and partial updates
- **Chart.js 4.x**: Trend chart rendering on the project dashboard
- **CSS**: Hand-crafted component classes (Tailwind-compatible utility names)

### Package Structure

```
app/web/
├── __init__.py          # Package marker
├── router.py            # Mounts all web sub-routers
├── dependencies.py      # Web-specific FastAPI dependencies (auth, CSRF)
├── context.py           # WebContext — shared template context builder
├── auth.py              # Login/register/logout routes + cookie helpers
├── pages.py             # Root redirect, workspace dashboard, project dashboard
├── onboarding.py        # Guided project creation wizard
├── scans.py             # Run measurement, polling, scan detail
├── opportunities.py     # Action Center — list, detail, transitions, verify
├── schedule.py          # Enable/disable scheduled measurements
├── notifications.py     # Notification center + preferences
├── project_config.py    # Project settings, prompt regeneration
├── dashboard_service.py # Read-only orchestration service (DashboardQueryService)
├── view_models.py       # Enum-to-label translations + formatting helpers
└── forms.py             # Onboarding wizard form parsing
```

### Template Structure

```
app/templates/
├── base.html            # Minimal HTML shell (for auth pages)
├── auth_base.html       # Auth page layout (login, register)
├── app_base.html        # Authenticated app layout (sidebar, topbar, CSRF meta)
├── auth/
│   ├── login.html
│   └── register.html
├── dashboard/
│   ├── workspace.html   # Workspace overview (project list, quota)
│   └── no_workspace.html
├── projects/
│   ├── dashboard.html   # Project dashboard (KPIs, trend, leaderboard)
│   └── onboarding.html  # Multi-step creation wizard
├── scans/
│   └── detail.html      # Scan detail with HTMX polling
├── partials/
│   └── scan_status.html # Polling partial for scan status
├── opportunities/
│   ├── list.html        # Action Center
│   └── detail.html      # Opportunity detail + workflow actions
├── notifications/
│   └── list.html
├── settings/
│   └── notifications.html
└── errors/
    ├── 404.html
    ├── 403.html
    └── 500.html
```

### Static Assets

```
app/static/
├── css/
│   ├── app.css              # Production CSS (committed)
│   └── tailwind-input.css   # Tailwind source (for rebuilds)
├── js/
│   └── app.js               # HTMX CSRF config
└── vendor/
    ├── htmx.min.js          # HTMX 2.0.4
    └── chart.umd.min.js     # Chart.js 4.4.x
```

## Authentication

Web auth uses the **same session cookie** as the API. The cookie is set by
`set_session_cookie()` in `app/web/auth.py`, which is shared with the API
auth router to ensure identical security behavior.

- **Login**: `GET /login` renders the form; `POST /login` processes it
- **Register**: `GET /register` renders the form; `POST /register` processes it
- **Logout**: `POST /logout` clears the cookie and revokes the session

CSRF protection applies to all state-changing requests. Login and register
routes are exempt (they establish the session). HTMX requests include the
CSRF token via the `X-CSRF-Token` header, configured by `app.js`.

## Route Map

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Root — redirects to `/app` or `/login` |
| `/login` | GET/POST | Login page and form processing |
| `/register` | GET/POST | Registration page and form processing |
| `/logout` | POST | Logout (clears cookie, revokes session) |
| `/app` | GET | Workspace dashboard (or no-workspace state) |
| `/app/w/{workspace_id}` | GET | Workspace overview dashboard |
| `/app/w/{workspace_id}/projects/new` | GET/POST | Onboarding wizard |
| `/app/w/{workspace_id}/projects/{project_id}` | GET | Project dashboard |
| `/app/w/{workspace_id}/projects/{project_id}/scans` | POST | Run measurement |
| `/app/w/{workspace_id}/projects/{project_id}/scans/{scan_id}` | GET | Scan detail |
| `/app/w/{workspace_id}/projects/{project_id}/scans/{scan_id}/status` | GET | Polling endpoint |
| `/app/w/{workspace_id}/projects/{project_id}/opportunities` | GET | Action Center |
| `/app/w/{workspace_id}/projects/{project_id}/opportunities/{id}` | GET | Opportunity detail |
| `/app/w/{workspace_id}/projects/{project_id}/opportunities/{id}/transition` | POST | Workflow transition |
| `/app/w/{workspace_id}/projects/{project_id}/opportunities/{id}/verify` | POST | Start verification |
| `/app/w/{workspace_id}/projects/{project_id}/schedule/enable` | POST | Enable schedule |
| `/app/w/{workspace_id}/projects/{project_id}/schedule/disable` | POST | Disable schedule |
| `/app/w/{workspace_id}/notifications` | GET | Notification center |
| `/app/w/{workspace_id}/notifications/{id}/read` | POST | Mark notification read |
| `/app/w/{workspace_id}/notifications/mark-all-read` | POST | Mark all read |
| `/app/w/{workspace_id}/settings/notifications` | GET/POST | Notification preferences |

## DashboardQueryService

The `DashboardQueryService` (`app/web/dashboard_service.py`) is a read-only
orchestration service that aggregates data from multiple backend services
for template rendering. It does NOT write to the database or mutate state.

Key methods:
- `get_workspace_overview()` — project list with latest metrics
- `get_project_dashboard()` — KPIs, trend points, leaderboard, providers
- `get_scan_detail()` — scan with metrics and prompt run details
- `get_scan_status()` — lightweight status query for polling
- `get_opportunity_list()` — filtered opportunity list
- `get_opportunity_detail()` — opportunity with verification info

## Development

### Rebuilding CSS

The committed `app.css` is the production artifact. To rebuild with full
Tailwind utilities:

```bash
npm install
npm run build:css
```

This requires Node.js and npm. The `tailwind.config.js` scans
`app/templates/**/*.html` and `app/static/js/**/*.js` for class usage.

### Running Locally

```bash
# Start the backend (API + web)
uvicorn app.main:app --reload --port 8000

# Access the web app
open http://localhost:8000/login
```

### Testing

Web layer unit tests are in `tests/unit/test_web_*.py`:
- `test_web_view_models.py` — label translations and formatting
- `test_web_forms.py` — onboarding form parsing
- `test_web_routes.py` — route registration and static file serving

Integration tests (requiring PostgreSQL + Redis) are in
`tests/integration/test_web_*.py`.
