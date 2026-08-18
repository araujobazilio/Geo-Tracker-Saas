# Project Onboarding

> **Source files:** `app/services/project_onboarding_service.py`, `app/services/project_service.py`, `app/services/tracking_service.py`, `app/services/project_provider_service.py`, `app/routers/api/projects.py`

## 1. Overview

A **Project** is the central tracking entity in geo-tracker. Each project
represents a brand whose presence in LLM responses is being monitored. A
project bundles together:

- **Brand & market configuration** — who the brand is, what industry it
  operates in, and which audience/region/language it targets.
- **Keywords** — the search phrases used to probe LLMs for brand visibility.
- **Competitors** — rival brands whose visibility is tracked alongside the
  primary brand.
- **Enabled providers** — the set of LLM providers (OpenAI, Anthropic, Google,
  Perplexity) against which prompts are run.

Onboarding is the act of creating a fully-configured project in a single
atomic operation. The result is a project with an initial **PromptSet v1**
ready for scanning.

## 2. Onboarding Flow

Project creation is handled by `ProjectOnboardingService.onboard_project()`.
The operation is **atomic**: if any step fails, the entire transaction rolls
back — no partial project, no orphan keywords, no half-created prompt set is
left behind.

### Steps

| Step | Description |
|------|-------------|
| 1 | **Validate & normalize inputs** — all fields are normalized *before* any row lock is acquired. |
| 2 | **Lock Workspace row** (`SELECT ... FOR UPDATE`) — prevents two concurrent onboarding requests from exceeding `max_projects`. |
| 3 | **Check project capacity** — `count_tracked_by_workspace` (ACTIVE + PAUSED) must be below `max_projects`. |
| 4 | **Create Project** — with normalized domain, brand, and market config. Initial `prompt_input_revision = 1`. |
| 5 | **Add Keywords** — each keyword is normalized, deduplicated by `normalized_text`, and capacity-checked. |
| 6 | **Add Competitors** — each competitor domain is normalized, must not match the project domain, deduplicated, and capacity-checked. |
| 7 | **Add Providers** — each provider is entitlement-checked against the plan's `allowed_providers`. |
| 8 | **Generate initial PromptSet v1** — deterministic prompts derived from the keyword/competitor/market config. |
| 9 | **Commit** — the entire transaction is committed in one shot. |

On any exception (`ValidationError`, `QuotaExceededError`, `ConflictError`,
or any other error), the session is rolled back and the exception re-raised.

```python
from app.services.project_onboarding_service import (
    ProjectOnboardingService,
    OnboardingRequest,
    KeywordInput,
    CompetitorInput,
)
from app.core.enums import LLMProvider

request = OnboardingRequest(
    name="Acme GEO Tracking",
    domain="https://www.acme.com",
    brand_name="Acme",
    brand_aliases=["Acme Inc", "Acme Corp"],
    industry="SaaS",
    target_country="US",
    target_language="en",
    target_audience="B2B marketers",
    keywords=[
        KeywordInput(text="best CRM software", funnel_stage="AWARENESS"),
        KeywordInput(text="CRM for small business", funnel_stage="CONSIDERATION"),
    ],
    competitors=[
        CompetitorInput(name="Globex", domain="https://globex.com"),
    ],
    providers=[LLMProvider.OPENAI, LLMProvider.ANTHROPIC],
)

service = ProjectOnboardingService(session)
project = service.onboard_project(
    workspace_id=workspace_id,
    request=request,
    created_by_user_id=user.id,
)
# project.prompt_input_revision == 1
# An initial PromptSet v1 has been generated.
```

### Concurrency safety

The Workspace row is locked with `FOR UPDATE` before the capacity check.
This ensures that two concurrent onboarding requests for the same workspace
cannot both pass the capacity check and exceed `max_projects`.

## 3. Project Configuration Fields

All configuration fields are defined on the `Project` model and validated
by `ProjectCreateRequest` / `ProjectUpdateRequest` schemas.

| Field | Type | Required | Normalization | Max length |
|-------|------|----------|---------------|------------|
| `name` | `str` | Yes | Trim | 255 |
| `domain` | `str` | Yes | `normalize_domain()` | 253 (hostname) |
| `brand_name` | `str` | Yes | Trim | 255 |
| `brand_aliases` | `list[str]` | No | `normalize_brand_aliases()` | 50 aliases, 255 chars each |
| `industry` | `str` | No | Trim | 255 |
| `target_country` | `str` | No | `normalize_country()` — uppercase 2-letter ISO code (e.g. `US`, `BR`) | 2 chars |
| `target_language` | `str` | No | `normalize_language()` — lowercased `xx` or `xx-yy` (e.g. `en`, `pt-br`) | 10 chars |
| `target_audience` | `str` | No | Trim | 255 |

### Domain normalization

`normalize_domain()` produces a canonical hostname:

- Strips scheme (`http://`, `https://`)
- Strips path, query, fragment
- Strips userinfo (`user:pass@`)
- Strips port
- Lowercases
- Strips trailing dot
- Strips leading `www.`
- Validates hostname format (labels, length, hyphens)

```
"https://www.Example.com:8443/path?q=1"  →  "example.com"
```

### Brand alias normalization

`normalize_brand_aliases()` trims whitespace, removes empty strings,
deduplicates case-insensitively (preserving first-seen display form), and
enforces a maximum of 50 aliases at 255 characters each.

### Country & language

- **Country** must be a 2-letter ISO code, uppercased (`"us"` → `"US"`).
- **Language** must be a supported code for deterministic prompt generation.
  Supported codes: `en`, `en-us`, `en-gb`, `pt`, `pt-br`, `pt-pt`. These map
  to language families (`en`, `pt`) used by the prompt generator.

## 4. Keywords

Keywords are the search phrases used to probe LLMs for brand visibility.

### Normalization

Each keyword is processed by `normalize_keyword()`, which returns a tuple of
`(display_text, normalized_text)`:

- **`display_text`** — outer whitespace trimmed, internal whitespace
  collapsed to single spaces. Preserves original casing.
- **`normalized_text`** — lowercased version of `display_text`, used for
  uniqueness enforcement.

```python
"  Best   CRM Software  "  →  ("Best CRM Software", "best crm software")
```

Maximum keyword length is 500 characters.

### Metadata fields

| Field | Type | Description |
|-------|------|-------------|
| `text` | `str` | The keyword phrase (immutable after creation). |
| `normalized_text` | `str` | Lowercased form for uniqueness. |
| `intent` | `str \| None` | Free-text commercial intent label (max 255 chars). |
| `funnel_stage` | `FunnelStage \| None` | One of `AWARENESS`, `CONSIDERATION`, `PURCHASE`. |

### Capacity limits

Keyword capacity is plan-based: `max_keywords_per_project` from
`EffectiveEntitlements`. The check is performed via
`EntitlementService.require_keyword_capacity()`, which raises
`QuotaExceededError` (HTTP 429) if the limit is reached.

### Uniqueness

Keywords are unique per project by `normalized_text`. Adding a duplicate
raises `ConflictError` (HTTP 409).

### Revision impact

Adding, updating (intent/funnel_stage/active), or deactivating a keyword
increments `prompt_input_revision` on the project. Keyword **text is
immutable** — to change the text, delete and re-add.

## 5. Competitors

Competitors are rival brands tracked alongside the primary brand.

### Domain normalization

Competitor domains are normalized with the same `normalize_domain()` as
project domains (strip scheme, `www`, path, port; lowercase; validate).

### Constraints

- **Cannot match project domain** — a competitor domain equal to the
  project's own domain raises `ValidationError` (HTTP 422).
- **Unique per project** — duplicate competitor domains within the same
  project raise `ConflictError` (HTTP 409).
- **Name required** — competitor name must be non-empty (max 255 chars).
- **Aliases** — normalized via `normalize_brand_aliases()` (max 50, 255
  chars each).

### Capacity limits

Competitor capacity is plan-based: `max_competitors_per_project` from
`EffectiveEntitlements`. Checked via
`EntitlementService.require_competitor_capacity()`.

### Revision impact

Adding, updating (name/aliases/active), or deactivating a competitor
increments `prompt_input_revision`. Competitor **domain is immutable** —
to change the domain, delete and re-add.

## 6. Providers

Each project has a set of enabled LLM providers against which prompts are
run. Supported providers:

| Enum | Value |
|------|-------|
| `LLMProvider.OPENAI` | `OPENAI` |
| `LLMProvider.ANTHROPIC` | `ANTHROPIC` |
| `LLMProvider.GOOGLE` | `GOOGLE` |
| `LLMProvider.PERPLEXITY` | `PERPLEXITY` |

### Entitlement enforcement

Every enabled provider **must** be in the workspace's plan-defined
`allowed_providers` set. This is checked via
`EntitlementService.require_provider()`, which raises
`EntitlementDeniedError` (HTTP 403) if the provider is not available on
the current plan.

### PUT replace semantics

Setting providers is a **full replacement** (`PUT`), not a patch. The
entire existing provider set is deleted and replaced with the new set.

### Plan downgrade behavior

If a provider disappears from the plan after a downgrade, the project
configuration is **not mutated**. The API exposes `enabled` and
`allowed_by_plan` separately in `ProviderResponse`. The Scan Engine uses
the intersection of project-enabled providers and effective allowed
providers.

### Revision impact

**Provider changes do NOT increment `prompt_input_revision`.** Providers
affect *where* prompts are sent, not *what* the prompt text is.

## 7. Project Status Lifecycle

Projects have three statuses defined in `ProjectStatus`:

```
ACTIVE  ⇄  PAUSED
  ↕
ARCHIVED
```

| Transition | Method | Capacity check? | Notes |
|------------|--------|-----------------|-------|
| `ACTIVE → PAUSED` | `pause_project()` | No | Idempotent — pausing an already-paused project is a no-op. |
| `PAUSED → ACTIVE` | `activate_project()` | No | Idempotent. |
| `ACTIVE → ARCHIVED` | `archive_project()` | No | Frees capacity. No hard delete. |
| `PAUSED → ARCHIVED` | `archive_project()` | No | Any non-archived status can be archived. |
| `ARCHIVED → ACTIVE` | `activate_project()` | **Yes** | Re-checks `max_projects` with Workspace row lock. |
| `ARCHIVED → PAUSED` | — | — | Not directly supported; activate first. |

### Invalid transitions

- Pausing a non-ACTIVE, non-PAUSED project raises `ConflictError`.
- Activating a project with an unrecognized status raises `ConflictError`.
- Archiving is always allowed (idempotent if already archived).

### Audit events

Every status transition records an audit event:

| Action | Trigger |
|--------|---------|
| `PROJECT_PAUSED` | `POST .../pause` |
| `PROJECT_ACTIVATED` | `POST .../activate` |
| `PROJECT_ARCHIVED` | `POST .../archive` |

## 8. Capacity Enforcement

Project capacity is governed by `max_projects` from the workspace's plan.

### Counted vs. uncounted

`ProjectRepository.count_tracked_by_workspace()` counts only **ACTIVE** and
**PAUSED** projects. **ARCHIVED projects do not consume capacity.**

```python
# From app/repositories/project_repository.py
Project.status.in_([ProjectStatus.ACTIVE, ProjectStatus.PAUSED])
```

### When capacity is checked

| Operation | Capacity check |
|-----------|----------------|
| **Onboard new project** | Yes — locks Workspace row, checks before creating. |
| **Activate from ARCHIVED** | Yes — locks Workspace row, re-checks before activating. |
| **Activate from PAUSED** | No — the project already counts toward capacity. |
| **Pause** | No — project still counts (PAUSED is tracked). |
| **Archive** | No — archiving *frees* capacity. |

If the limit is exceeded, `QuotaExceededError` (HTTP 429) is raised:

> Project limit reached (N). Upgrade your plan to create more projects.

## 9. Input Revision Tracking

`prompt_input_revision` is a monotonically increasing counter on the
`Project` model. It tracks changes to inputs that affect **prompt text
generation**. When the revision changes, the current PromptSet becomes
**stale** (`is_stale = current_set.input_revision != project.prompt_input_revision`)
and should be regenerated.

### What increments the revision

| Change | Increments? |
|--------|-------------|
| Project `domain` | Yes |
| Project `brand_name` | Yes |
| Project `brand_aliases` | Yes |
| Project `industry` | Yes |
| Project `target_country` | Yes |
| Project `target_language` | Yes |
| Project `target_audience` | Yes |
| Project `name` | **No** — name is cosmetic, doesn't affect prompts. |
| Keyword added | Yes |
| Keyword updated (intent/funnel_stage/active) | Yes |
| Competitor added | Yes |
| Competitor updated (name/aliases/active) | Yes |
| **Provider changes** | **No** — providers don't affect prompt text. |

### How it works

On project update (`ProjectService.update_project()`), each prompt-affecting
field is compared against its current normalized value. The revision is
incremented **only if at least one field actually changed** (normalized
comparison, not just "field was present in the request").

```python
# From app/services/project_service.py
if revision_changed:
    project.prompt_input_revision += 1
```

Onboarding sets the initial revision to `1`. The initial PromptSet v1 is
generated at `input_revision = 1`.

### Staleness detection

The project summary endpoint (`GET .../projects/{pid}`) returns
`is_prompt_set_stale`, which compares the current active PromptSet's
`input_revision` against the project's `prompt_input_revision`. If they
differ, the prompt set is stale and `POST .../prompt-sets/regenerate`
should be called.

## 10. API Endpoints

All endpoints are mounted under:

```
/api/v1/workspaces/{workspace_id}/projects
```

### Authorization

| Operation type | Required role |
|----------------|---------------|
| Read (GET) | Membership (any role: OWNER, ADMIN, MEMBER) |
| Write (POST, PATCH, PUT) | ADMIN or OWNER |

Cross-workspace access (a user who is not a member of the workspace)
raises `TenantAccessError`, which is translated to **HTTP 404** (not 403)
to avoid revealing whether an inaccessible resource exists. A
`TENANT_ACCESS_DENIED` audit event is recorded.

### Project endpoints

| Method | Path | Description | Role |
|--------|------|-------------|------|
| `POST` | `/` | Onboard a new project (atomic). | ADMIN+ |
| `GET` | `/` | List all projects in workspace. | Member |
| `GET` | `/{project_id}` | Get project summary (counts, staleness, scan estimate). | Member |
| `PATCH` | `/{project_id}` | Update project configuration. | ADMIN+ |
| `POST` | `/{project_id}/pause` | Pause project. | ADMIN+ |
| `POST` | `/{project_id}/activate` | Activate (from PAUSED or ARCHIVED). | ADMIN+ |
| `POST` | `/{project_id}/archive` | Archive project (frees capacity). | ADMIN+ |

### Keyword endpoints

| Method | Path | Description | Role |
|--------|------|-------------|------|
| `GET` | `/{project_id}/keywords` | List keywords. | Member |
| `POST` | `/{project_id}/keywords` | Add keyword. | ADMIN+ |
| `PATCH` | `/{project_id}/keywords/{keyword_id}` | Update keyword (intent/funnel_stage/active). | ADMIN+ |

### Competitor endpoints

| Method | Path | Description | Role |
|--------|------|-------------|------|
| `GET` | `/{project_id}/competitors` | List competitors. | Member |
| `POST` | `/{project_id}/competitors` | Add competitor. | ADMIN+ |
| `PATCH` | `/{project_id}/competitors/{competitor_id}` | Update competitor (name/aliases/active). | ADMIN+ |

### Provider endpoints

| Method | Path | Description | Role |
|--------|------|-------------|------|
| `GET` | `/{project_id}/providers` | List providers (with `allowed_by_plan` status). | Member |
| `PUT` | `/{project_id}/providers` | Replace enabled provider set. | ADMIN+ |

### Prompt set endpoints

| Method | Path | Description | Role |
|--------|------|-------------|------|
| `GET` | `/{project_id}/prompt-sets` | List all prompt sets. | Member |
| `GET` | `/{project_id}/prompt-sets/current` | Get current active prompt set with prompts. | Member |
| `GET` | `/{project_id}/prompt-sets/{version}` | Get a specific version with prompts. | Member |
| `POST` | `/{project_id}/prompt-sets/regenerate` | Regenerate prompt set (creates new version). | ADMIN+ |

### Example: Onboard a project

```http
POST /api/v1/workspaces/{workspace_id}/projects
Content-Type: application/json
Authorization: Bearer <token>

{
  "name": "Acme GEO Tracking",
  "domain": "https://www.acme.com",
  "brand_name": "Acme",
  "brand_aliases": ["Acme Inc", "Acme Corp"],
  "industry": "SaaS",
  "target_country": "US",
  "target_language": "en",
  "target_audience": "B2B marketers",
  "keywords": [
    {"text": "best CRM software", "funnel_stage": "AWARENESS"},
    {"text": "CRM for small business", "funnel_stage": "CONSIDERATION"}
  ],
  "competitors": [
    {"name": "Globex", "domain": "https://globex.com"}
  ],
  "providers": ["OPENAI", "ANTHROPIC"]
}
```

**Response** — `201 Created`:

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "workspace_id": "...",
  "name": "Acme GEO Tracking",
  "domain": "acme.com",
  "brand_name": "Acme",
  "brand_aliases": ["Acme Inc", "Acme Corp"],
  "industry": "SaaS",
  "target_country": "US",
  "target_language": "en",
  "target_audience": "B2B marketers",
  "status": "ACTIVE",
  "prompt_input_revision": 1,
  "last_scan_at": null,
  "created_at": "2026-01-15T12:00:00Z",
  "updated_at": "2026-01-15T12:00:00Z"
}
```

### Error responses

| HTTP status | Exception | When |
|-------------|-----------|------|
| `404` | `TenantAccessError` | User is not a member of the workspace. |
| `403` | `AuthorizationError` | User is a member but lacks ADMIN/OWNER role. |
| `403` | `EntitlementDeniedError` | Provider not allowed on current plan. |
| `409` | `ConflictError` | Project not found in workspace, or duplicate keyword/competitor. |
| `422` | `ValidationError` | Invalid input (empty name, bad domain, competitor matches project domain). |
| `429` | `QuotaExceededError` | Project/keyword/competitor capacity exceeded. |
