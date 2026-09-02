# Dashboard UX

The GEO Tracker dashboard provides a B2B SaaS interface for monitoring
AI visibility metrics. It has two levels: workspace overview and
project dashboard.

## Workspace Overview

**Route**: `GET /app/w/{workspace_id}`

Displays:
- **Project list** — all active projects in the workspace with:
  - Project name and domain
  - Latest measurement visibility rate
  - Measurement coverage
  - High-priority opportunity count
  - Last measurement date
- **Quota indicator** — AI Checks used / limit with warning state at 80%+
- **Plan name** — current subscription plan
- **Create project CTA** — links to the onboarding wizard (ADMIN only)

### Empty State
When the workspace has no projects, a call-to-action card prompts the
user to create their first project.

## Project Dashboard

**Route**: `GET /app/w/{workspace_id}/projects/{project_id}`

Displays:

### KPI Cards
- **Visibility rate** — percentage of AI responses that mention the brand
- **Citation rate** — percentage of mentions that link to the brand's domain
- **Discovery rate** — percentage of non-branded queries where the brand appears
- **Coverage** — percentage of planned prompts that completed successfully

Each KPI shows "Not enough data" when no measurement has completed.

### Trend Chart
- Line chart showing visibility and citation rates over time
- One data point per completed measurement
- Rendered with Chart.js (loaded from `/static/vendor/chart.umd.min.js`)
- Data is embedded as JSON in the template (no API call needed)

### Provider Leaderboard
- Table of AI providers with their visibility and citation rates
- Sorted by visibility rate (highest first)
- Shows "Not enough data" for providers without completed observations

### Action Center Summary
- Count of open opportunities by priority
- Link to the full Action Center page

### Measurement Actions
- **Run measurement** button (ADMIN/OWNER only)
- Checks prompt set staleness — refuses if prompts are stale
- Uses idempotency key to prevent duplicate submissions
- Redirects to scan detail page after creation

### Schedule Section
- Shows current schedule status (enabled/disabled, interval)
- Enable/disable controls (ADMIN/OWNER only)
- Interval is constrained by `min_scheduled_scan_interval_hours` entitlement
- Shows skip reasons if the last scheduled scan was skipped

## Scan Detail Page

**Route**: `GET /app/w/{workspace_id}/projects/{project_id}/scans/{scan_id}`

Displays:
- Scan type, status, created/completed timestamps
- **HTMX polling** — status updates every 4 seconds for non-terminal scans
- Status badge with color coding (gray=queued, blue=running, green=completed,
  yellow=partial, red=failed)

### Polling Endpoint
`GET /app/w/{workspace_id}/projects/{project_id}/scans/{scan_id}/status`

Returns a partial HTML template (`partials/scan_status.html`) with the
current status. The partial includes `hx-trigger="every 4s"` for
non-terminal scans, which stops polling once the scan reaches a terminal
state (COMPLETED, PARTIAL, FAILED, CANCELED).

## View Models

All internal enum values are translated to customer-facing labels via
`app/web/view_models.py`. Internal terminology (PromptRun, UsageEvent,
QuotaReservation) is never exposed to the user.

Key translations:
- `STANDARD` scan → "Measurement"
- `CONFIDENCE` scan → "Reliability check"
- `VERIFICATION` scan → "Verification"
- `FAILED` prompt run → "Measurement unavailable"
- `SKIPPED_QUOTA` → "Skipped because the workspace did not have enough AI Checks"

## Quota Display

The quota indicator in the topbar shows:
- AI Checks used + reserved / limit
- Warning state (yellow) at 80%+ usage
- Hidden on mobile (responsive)

## Role-Based UI

- **OWNER/ADMIN**: Can create projects, run measurements, manage schedules,
  transition opportunities, start verifications
- **MEMBER**: Read-only access to dashboards and Action Center
- Role is displayed in the sidebar footer
