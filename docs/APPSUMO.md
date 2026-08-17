# AppSumo Integration

## Status

**PLANNED** (Phase 14). The Phase 1 data model establishes the
foundational structures (`appsumo_licenses`, `billing_accounts` with
`APPSUMO` source, `provider_webhook_events` for idempotent webhook
ingestion) but does NOT implement the AppSumo API integration.

## Why first-class

AppSumo support is a first-class architectural requirement, not an
afterthought. GEO Tracker is intended to launch via an AppSumo Lifetime
Deal, so licensing/redemption must be considered from the start.

## Data model (IMPLEMENTED in Phase 1)

### `appsumo_licenses`

| Field | Description |
|-------|-------------|
| `id` | UUID PK |
| `workspace_id` | Owning workspace (RESTRICT on delete — license history retained) |
| `billing_account_id` | Optional link to the billing account (SET NULL on delete) |
| `external_license_id` | AppSumo's license identifier |
| `appsumo_plan` | AppSumo plan/tier code |
| `status` | ACTIVE / INACTIVE / SUSPENDED |
| `activated_at` | Activation timestamp |
| `deactivated_at` | Deactivation timestamp |
| `metadata` | JSONB for extra fields from AppSumo |

Licenses have their own model (never stored only on the User) so
lifecycle events can be tracked independently and license history
survives user/workspace changes.

### `billing_accounts` (source = APPSUMO)

A workspace's AppSumo entitlement is represented as a `BillingAccount`
with `source = APPSUMO`, linked to one or more `AppSumoLicense` records.

### `provider_webhook_events`

Generic, idempotent webhook event store. `(provider, external_event_id)`
is unique, so a replayed AppSumo webhook is detected and not re-processed.

## Planned integration (Phase 14)

Potential endpoints:

- `/integrations/appsumo/oauth/callback`
- `/integrations/appsumo/webhook`

Webhook processing requirements:

- validated
- authenticated (signature verification)
- idempotent (via `provider_webhook_events`)
- auditable
- replay-safe
- retry-safe

Conceptual lifecycle events: `purchase`, `activation`, `upgrade`,
`downgrade`, `deactivation`.

**IMPORTANT:** AppSumo's official integration documentation MUST be
verified during Phase 14 implementation. Do not rely on assumptions.

## AppSumo tier → internal plan mapping

A dedicated service maps an AppSumo tier to internal plan/entitlements.
This mapping is configuration-driven, not hardcoded in domain logic.
See `docs/ENTITLEMENTS.md`.
