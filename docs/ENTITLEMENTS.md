# Entitlements

## Status

**IMPLEMENTED** (Phase 3). Entitlement resolution is implemented via
`EntitlementService` and `PlanDefinition`. Quota enforcement is
implemented via `QuotaService` (see `docs/USAGE_AND_QUOTAS.md`).

The Phase 1 data model established the foundational structures
(`billing_accounts`, `appsumo_licenses`). Phase 3 adds the
`PlanDefinition` model, the `EntitlementService`, and the
`EffectiveEntitlements` value object.

## Principles

Entitlements (what a workspace can do) are **configuration-driven**, not
hardcoded in domain logic. Plan limits are NEVER baked into controllers
or models.

All entitlement checks go through a single service boundary:

```
EntitlementService.get_effective_entitlements(workspace_id)
EntitlementService.is_provider_allowed(workspace_id, provider)
EntitlementService.require_provider(workspace_id, provider)
EntitlementService.require_feature(workspace_id, feature)
EntitlementService.require_project_capacity(workspace_id, count)
```

Business logic must NEVER scatter checks like `if user.tier == "tier_2"`.

## PlanDefinition model

`PlanDefinition` is the source of truth for plan limits and feature
flags. It is a typed, structured model — NEVER stored as an unstructured
JSON blob.

| Column | Type | Notes |
|--------|------|-------|
| `code` | String(80) | Unique plan code |
| `name` | String(255) | Display name |
| `description` | String(1000), nullable | Human description |
| `is_active` | Boolean | Inactive plans resolve to UNENTITLED |
| `max_projects` | Integer | CHECK `>= 0` |
| `max_keywords_per_project` | Integer | CHECK `>= 0` |
| `max_competitors_per_project` | Integer | CHECK `>= 0` |
| `max_team_members` | Integer | CHECK `>= 0` |
| `monthly_ai_checks` | Integer | CHECK `>= 0`, always finite (never unlimited) |
| `min_scheduled_scan_interval_hours` | Integer, nullable | NULL = no scheduled scans; if set, CHECK `> 0` |
| `confidence_scans_enabled` | Boolean | Feature flag |
| `verification_scans_enabled` | Boolean | Feature flag |
| `white_label_reports` | Boolean | Feature flag |
| `exports_enabled` | Boolean | Feature flag |
| `agency_dashboard` | Boolean | Feature flag |
| `integrations_enabled` | Boolean | Feature flag |
| `byok_enabled` | Boolean | Feature flag |

Key design decisions:

1. Plan limits are **typed columns with CHECK constraints**, NOT JSON
   blobs. This keeps limits queryable, validated at the database level,
   and type-safe in Python.
2. `monthly_ai_checks` is **always a finite non-negative integer**
   (never `NULL`, never `-1`, never "unlimited"). Unlimited AI usage is
   not supported to protect paid-provider API economics.
3. An inactive `PlanDefinition` (`is_active = false`) resolves to
   UNENTITLED, allowing plans to be retired without deleting history.

## Provider allowlist (`plan_providers`)

Allowed AI providers for a plan are stored as rows in the `plan_providers`
association table (unique on `plan_id` + `provider`).

**An empty provider set means NO providers are allowed** — it must NOT
implicitly mean "all providers". This is a deliberate fail-closed
semantic: a new or misconfigured plan grants nothing by default.

## EntitlementService

`EntitlementService` (`app/services/entitlement_service.py`) is the
single point of entitlement resolution. Routers and services consume
`EffectiveEntitlements`, never billing tables directly.

### Resolution chain

```
BillingAccount (primary, eligible status) → plan_code → PlanDefinition → EffectiveEntitlements
```

1. Load the **primary** `BillingAccount` for the workspace.
2. Verify the billing account status is eligible (`ACTIVE` or
   `TRIALING`).
3. Resolve `plan_code` to a `PlanDefinition`.
4. Verify the plan is active.
5. Load the allowed providers from `plan_providers`.
6. Build an `EffectiveEntitlements` snapshot.

### Fail-safe: UNENTITLED

`get_effective_entitlements` **never raises**. It returns a conservative
`UNENTITLED` snapshot (all limits zero, all flags false, no providers)
when any of the following is true:

- No primary `BillingAccount` exists for the workspace.
- The primary billing account status is not eligible (e.g. `CANCELED`,
  `PAST_DUE`).
- The billing account has no `plan_code`.
- The `plan_code` does not match any `PlanDefinition`.
- The `PlanDefinition` is inactive.

This fail-closed behavior ensures a misconfigured or lapsed workspace
cannot accidentally access paid capabilities.

## EffectiveEntitlements value object

`EffectiveEntitlements` (`app/core/entitlements.py`) is an immutable
`NamedTuple` snapshot of what a workspace is entitled to. The rest of
the application consumes this object, never billing/plan tables.

| Field | Type |
|-------|------|
| `workspace_id` | UUID |
| `plan_code` | str (`"UNENTITLED"` when fail-closed) |
| `billing_source` | `BillingSource \| None` |
| `max_projects` | int |
| `max_keywords_per_project` | int |
| `max_competitors_per_project` | int |
| `max_team_members` | int |
| `monthly_ai_checks` | int |
| `allowed_providers` | `frozenset[LLMProvider]` |
| `min_scheduled_scan_interval_hours` | int \| None |
| `confidence_scans_enabled` | bool |
| `verification_scans_enabled` | bool |
| `white_label_reports` | bool |
| `exports_enabled` | bool |
| `agency_dashboard` | bool |
| `integrations_enabled` | bool |
| `byok_enabled` | bool |

`is_unentitled` is a convenience property (`plan_code == "UNENTITLED"`).

## EntitlementDeniedError

`EntitlementDeniedError` (HTTP 403, code `entitlement_denied`) is raised
by `require_provider` and `require_feature` when a capability is not
available on the workspace's current plan. It is distinct from
`QuotaExceededError` (429), which is raised when the plan allows the
capability but the monthly quota is exhausted.

## Conceptual entitlements

| Entitlement | Description |
|-------------|-------------|
| `max_projects` | Maximum projects per workspace |
| `max_keywords_per_project` | Keyword cap per project |
| `max_competitors_per_project` | Competitor cap per project |
| `monthly_ai_checks` | Monthly AI Check allowance (always finite) |
| `allowed_providers` | Which AI providers are enabled (empty = none) |
| `min_scheduled_scan_interval_hours` | Minimum scan interval (NULL = no scheduled scans) |
| `confidence_scans_enabled` | Confidence scan access |
| `verification_scans_enabled` | Verification scan access |
| `white_label_reports` | White-label reporting access |
| `max_team_members` | Seat count |
| `exports_enabled` | Export access |
| `integrations_enabled` | Integration access |
| `byok_enabled` | Bring-your-own-key access |
| `agency_dashboard` | Agency dashboard access |

## AI Check (core usage unit)

**1 AI Check = one prompt executed once against one AI provider.**

Example: 5 prompts x 4 providers = 20 AI Checks.

Every external LLM request creates a `usage_events` record tracking
workspace, user, project, scan, provider, model, tokens, and estimated
USD cost. Quota enforcement is implemented in Phase 3 via `QuotaService`
(see `docs/USAGE_AND_QUOTAS.md`).

## Cost protection

AI usage is NEVER unbounded (critical for AppSumo Lifetime Deal).
`monthly_ai_checks` is always a finite integer on every plan. Before
every AI call, `QuotaService` validates entitlement and remaining
usage. See `docs/APPSUMO.md` for the licensing rationale and
`docs/USAGE_AND_QUOTAS.md` for the quota engine.

## Billing sources

A workspace's entitlement may originate from any of:

| Source | Description |
|--------|-------------|
| `APPSUMO` | AppSumo Lifetime Deal license |
| `STRIPE` | Direct Stripe subscription |
| `ADMIN` | Admin-granted access |

`BillingAccount` is independent from `User` so a workspace can be billed
without coupling to a single user. The **primary** billing account
(`is_primary = true`) is the one used for entitlement resolution; a
partial unique index ensures at most one primary per workspace at any
time. AppSumo tier → internal plan mapping is handled by a dedicated
service (Phase 14).

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/workspaces/{workspace_id}/entitlements` | Effective entitlements (product capabilities) |

The entitlements endpoint returns **product capabilities**, not billing
internals (no customer IDs, license IDs, or subscription details). It
requires authentication and workspace membership; cross-tenant access
returns 404.

## Initial commercial concept (placeholders, configurable)

| Tier | Concept |
|------|---------|
| Solo | 1-2 projects, all core providers, low AI Check allowance |
| Pro | Multiple projects, higher AI Checks, advanced reporting, Confidence Scans |
| Agency | Many projects, high AI Checks, agency dashboard, white-label, team seats |

These are planning placeholders only. Pricing and quotas are validated
before launch and remain configurable via `PlanDefinition` rows.
