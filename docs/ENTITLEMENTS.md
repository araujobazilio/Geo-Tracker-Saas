# Entitlements

## Status

**PLANNED** (Phase 3). The Phase 1 data model establishes the
foundational structures (`billing_accounts`, `appsumo_licenses`) but
does not yet implement entitlement evaluation or quota enforcement.

## Principles

Entitlements (what a workspace can do) are **configuration-driven**, not
hardcoded in domain logic. Plan limits are NEVER baked into controllers
or models.

All entitlement checks go through a single service boundary:

```
EntitlementService.can_create_project(...)
EntitlementService.can_add_keyword(...)
EntitlementService.can_run_ai_checks(...)
EntitlementService.can_generate_report(...)
EntitlementService.can_use_confidence_scan(...)
EntitlementService.can_use_white_label(...)
```

Business logic must NEVER scatter checks like `if user.tier == "tier_2"`.

## Conceptual entitlements

| Entitlement | Description |
|-------------|-------------|
| `max_projects` | Maximum projects per workspace |
| `max_keywords_per_project` | Keyword cap per project |
| `max_competitors` | Competitor cap per project |
| `monthly_ai_checks` | Monthly AI Check allowance |
| `allowed_providers` | Which AI providers are enabled |
| `scan_frequency` | Minimum scan interval |
| `confidence_scans` | Confidence scan access |
| `manual_verification_scans` | Verification scan access |
| `white_label_reports` | White-label reporting access |
| `team_members` | Seat count |
| `exports` | Export access |
| `integrations` | Integration access |
| `BYOK` | Bring-your-own-key access |
| `agency_dashboard` | Agency dashboard access |

## AI Check (core usage unit)

**1 AI Check = one prompt executed once against one AI provider.**

Example: 5 prompts × 4 providers = 20 AI Checks.

Every external LLM request creates a `usage_events` record tracking
workspace, user, project, scan, provider, model, tokens, and estimated
USD cost. This is implemented structurally in Phase 1; enforcement is
Phase 3.

## Cost protection

AI usage is NEVER unbounded (critical for AppSumo Lifetime Deal).
Before every AI call, `QuotaService` validates entitlement and remaining
usage. See `docs/APPSUMO.md` for the licensing rationale.

## Billing sources

A workspace's entitlement may originate from any of:

| Source | Description |
|--------|-------------|
| `APPSUMO` | AppSumo Lifetime Deal license |
| `STRIPE` | Direct Stripe subscription |
| `ADMIN` | Admin-granted access |

`BillingAccount` is independent from `User` so a workspace can be billed
without coupling to a single user. AppSumo tier → internal plan mapping
is handled by a dedicated service (Phase 14).

## Initial commercial concept (placeholders, configurable)

| Tier | Concept |
|------|---------|
| Solo | 1–2 projects, all core providers, low AI Check allowance |
| Pro | Multiple projects, higher AI Checks, advanced reporting, Confidence Scans |
| Agency | Many projects, high AI Checks, agency dashboard, white-label, team seats |

These are planning placeholders only. Pricing and quotas are validated
before launch and remain configurable.
