# Onboarding UX

The guided project onboarding wizard helps users create a new tracking
project in a single session. The wizard uses progressive sections in the
browser but submits **one final POST** to the backend — no partial project
records are created during the wizard steps.

## Wizard Flow

### Step 1: Brand
- **Project name** (required) — internal label for the project
- **Domain / website** (required) — the brand's primary domain
- **Brand name** (required) — the entity to track in AI responses
- **Brand aliases** (optional) — comma-separated alternative names
- **Industry** (optional) — e.g., "SaaS", "E-commerce"
- **Target audience** (optional) — e.g., "Developers", "SMB owners"
- **Target country** (optional) — ISO code, e.g., "US"
- **Target language** (optional) — ISO code, e.g., "en"

### Step 2: Topics
- Add one or more topics/questions that the brand wants to be mentioned for
- Each topic can have an **intent** (informational, commercial) and
  **funnel stage** (awareness, consideration, purchase)
- Limit: defined by `max_keywords_per_project` entitlement

### Step 3: Competitors (optional)
- Add competitor name, domain, and aliases
- Limit: defined by `max_competitors_per_project` entitlement

### Step 4: AI Providers
- Select which AI providers should measure the brand
- Available providers are filtered by the workspace's entitlement
- At least one provider must be selected

### Step 5: Review
- Summary of the configuration
- A measurement uses one AI Check for each successful prompt/provider
  observation
- User can run their first measurement after creating the project

## Validation

Validation happens server-side in `parse_onboarding_form()`:
- Project name, domain, and brand name are required
- At least one topic is required
- Invalid provider values are silently ignored
- Competitors are optional

If validation fails, the wizard re-renders with error messages and the
user's entered data preserved.

## Backend Processing

The `POST /app/w/{workspace_id}/projects/new` endpoint:
1. Requires `ADMIN` role
2. Parses the form data via `parse_onboarding_form()`
3. Validates the parsed data
4. Calls `ProjectOnboardingService.onboard_project()` with the complete payload
5. Records an audit event (`PROJECT_CREATED`)
6. Redirects to the new project's dashboard

No partial project data is created on validation error — the entire
project (with keywords, competitors, providers, and prompt set) is created
atomically in a single transaction.

## Entitlement Enforcement

The backend enforces entitlements:
- `max_keywords_per_project` — topics limit
- `max_competitors_per_project` — competitors limit
- `allowed_providers` — only entitled providers can be selected

The wizard displays these limits to set user expectations, but the backend
is the final authority.
