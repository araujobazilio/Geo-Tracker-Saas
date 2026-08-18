# Usage and Quotas

## Status

**IMPLEMENTED** (Phase 3). Quota enforcement for AI Checks is
implemented via `QuotaService`, backed by PostgreSQL row-level locking.

## Overview

AI usage is NEVER unbounded. Every AI Check is reserved before it runs
and committed after the provider call succeeds. `QuotaService`
(`app/services/quota_service.py`) is the single point of quota
enforcement.

```
Available AI Checks = limit - used - reserved
```

- `limit` comes from `EffectiveEntitlements.monthly_ai_checks` (always
  finite, resolved from `PlanDefinition`).
- `used` and `reserved` are aggregate counters on
  `WorkspaceUsagePeriod`.
- The immutable `usage_events` table remains the detailed ledger.

## Monthly quota period

The quota period is the **UTC calendar month** (e.g.
`2026-08-01 00:00:00+00:00` to `2026-09-01 00:00:00+00:00`). Domain
logic never relies on server local time. A new `WorkspaceUsagePeriod`
row is created (via upsert) the first time a workspace reserves quota
in a given month.

## PostgreSQL is the source of truth

**Redis is NOT the quota source of truth.** PostgreSQL is authoritative.
Quota mutations use `SELECT ... FOR UPDATE` row-level locking on the
`workspace_usage_periods` row to prevent race conditions where two
concurrent workers both see "100 available" and each reserves 80
(total 160 > limit 100).

## WorkspaceUsagePeriod model

`WorkspaceUsagePeriod` (`app/models/workspace_usage_period.py`) tracks
the aggregate AI Check usage for a workspace during a monthly period.

| Column | Type | Notes |
|--------|------|-------|
| `workspace_id` | UUID FK → `workspaces.id` (RESTRICT) | |
| `period_start` | DateTime(tz) | Start of UTC month |
| `period_end` | DateTime(tz) | Start of next UTC month |
| `ai_checks_used` | Integer | CHECK `>= 0` |
| `ai_checks_reserved` | Integer | CHECK `>= 0` |

- Unique on `(workspace_id, period_start)` — one period row per
  workspace per month.
- `workspace_id` FK is **RESTRICT** (usage history must survive
  workspace deletion).

## QuotaReservation model

`QuotaReservation` (`app/models/quota_reservation.py`) is an atomic
reservation of AI Checks for a workspace.

| Column | Type | Notes |
|--------|------|-------|
| `workspace_id` | UUID FK → `workspaces.id` (RESTRICT) | |
| `project_id` | UUID FK → `projects.id` (SET NULL), nullable | |
| `user_id` | UUID FK → `users.id` (SET NULL), nullable | |
| `idempotency_key` | String(255) | Unique — retry returns existing record |
| `ai_checks_reserved` | Integer | CHECK `> 0` (must reserve at least 1) |
| `ai_checks_committed` | Integer | CHECK `>= 0` and `<= ai_checks_reserved` |
| `status` | String(20) | `ACTIVE` / `COMMITTED` / `RELEASED` / `EXPIRED` |
| `expires_at` | DateTime(tz), nullable | TTL deadline for ACTIVE reservations |

Constraints:

- `uq_quota_reservations_idempotency_key` — unique idempotency key.
- `ck_quota_reservations_reserved_positive` — `ai_checks_reserved > 0`.
- `ck_quota_reservations_committed_non_negative` —
  `ai_checks_committed >= 0`.
- `ck_quota_reservations_committed_le_reserved` —
  `ai_checks_committed <= ai_checks_reserved`.

Reservations are **never deleted** after completion. They are retained
in `COMMITTED`, `RELEASED`, or `EXPIRED` status for operational and
accounting history.

## Reservation lifecycle

```
ACTIVE    → reservation created, quota held
COMMITTED → all reserved checks have been used (committed == reserved)
RELEASED  → unused reserved checks released back (scan canceled/failed)
EXPIRED   → stale reservation expired by cleanup, remaining released
```

```
        reserve_ai_checks()
              |
              v
           ACTIVE
           /     \
commit    /       \   release_reservation()
   v     /         \        v
COMMITTED          RELEASED
                        ^
                        |
              expire_stale_reservations()
                        v
                     EXPIRED
```

State transitions:

| From | To | Trigger |
|------|----|---------|
| (none) | `ACTIVE` | `reserve_ai_checks` |
| `ACTIVE` | `COMMITTED` | `commit_ai_checks` (committed == reserved) |
| `ACTIVE` | `RELEASED` | `release_reservation` |
| `ACTIVE` | `EXPIRED` | `expire_stale_reservations` (TTL passed) |

`COMMITTED`, `RELEASED`, and `EXPIRED` are terminal states. Releasing
or expiring an already-terminal reservation is an idempotent no-op.

## QuotaService methods

`QuotaService` (`app/services/quota_service.py`) controls its own
transaction boundaries for each mutation. Audit logging is independent
— accounting consistency never depends on audit success.

### `reserve_ai_checks(workspace_id, requested_checks, idempotency_key, ...)`

Atomically reserve AI Checks before a scan/provider call executes.

- Uses `SELECT ... FOR UPDATE` on the usage period row.
- Idempotent on `idempotency_key`: retrying with the same key returns
  the existing reservation. Reusing the same key with conflicting
  parameters raises `ConflictError`.
- Raises `QuotaExceededError` (429) if not enough quota remaining.
- Sets `expires_at = now + quota_reservation_ttl_seconds`.

### `commit_ai_checks(reservation_id, quantity, usage_idempotency_key, ...)`

Commit N AI Checks against a reservation after the provider call
succeeds. Atomically:

- Decrements `ai_checks_reserved`, increments `ai_checks_used` on the
  usage period.
- Increments `ai_checks_committed` on the reservation.
- If `committed == reserved`, marks the reservation `COMMITTED`.
- Creates an immutable `UsageEvent` linked via `quota_reservation_id`.

Idempotent on `usage_idempotency_key`: retrying returns the existing
`UsageEvent` without double-counting. Raises `ConflictError` if the
quantity exceeds the remaining uncommitted balance or the reservation
is not in an active/committed state.

### `release_reservation(reservation_id)`

Release remaining uncommitted reserved checks back to the pool (e.g.
scan canceled or failed). Sets the reservation to `RELEASED`.
Idempotent: calling twice does not subtract twice.

### `expire_stale_reservations()`

Expire `ACTIVE` reservations whose `expires_at` has passed. Releases
the remaining reserved balance back to the pool and sets each to
`EXPIRED`. Returns the count of reservations expired. Intended to be
called by a periodic Celery Beat job.

### `get_usage_snapshot(workspace_id)`

Returns a `UsageSnapshot` (immutable `NamedTuple`) for the current
monthly period:

| Field | Type |
|-------|------|
| `workspace_id` | UUID |
| `period_start` | DateTime |
| `period_end` | DateTime |
| `limit` | int |
| `used` | int |
| `reserved` | int |
| `remaining` | int (property: `max(0, limit - used - reserved)`) |
| `usage_percentage` | int (property: 0-100) |

## UsageEvent idempotency and traceability

`usage_events` gains two columns in Phase 3:

| Column | Type | Notes |
|--------|------|-------|
| `idempotency_key` | String(255), nullable | Unique — prevents double-counting on provider-call retries |
| `quota_reservation_id` | UUID, nullable | Plain UUID (no FK) linking to the originating reservation |

`idempotency_key` is unique when present (partial uniqueness enforced
via a `UNIQUE` constraint). A provider-call retry must not result in
double-counted AI Checks, tokens, or cost.

`quota_reservation_id` is a **plain UUID with no foreign key** (no
cascade). This ensures `UsageEvent` retention is not compromised if a
reservation row is ever deleted — billing/cost history must survive.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `quota_reservation_ttl_seconds` | `1800` (30 minutes) | TTL after which an ACTIVE reservation is considered stale and eligible for expiration |

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/workspaces/{workspace_id}/usage` | Current monthly AI Check quota state |

The usage endpoint returns `period_start`, `period_end`, `limit`,
`used`, `reserved`, `remaining`, and `usage_percentage`. It requires
authentication and workspace membership; cross-tenant access returns
404. It does not expose billing internals.

## Design decisions

1. **PostgreSQL is the quota source of truth**, NOT Redis. Redis may be
   used for caching but never for authoritative quota state.
2. **Row-level locking** (`SELECT ... FOR UPDATE`) prevents concurrent
   oversubscription.
3. **Quota reservations are retained** after completion
   (`COMMITTED`/`RELEASED`/`EXPIRED`) for history — they are never
   deleted.
4. **`UsageEvent.idempotency_key`** prevents double-counting on
   provider-call retries.
5. **`monthly_ai_checks` is always finite** (never unlimited) to
   protect paid-provider API economics.
6. **Monthly quota period = UTC calendar month.**
7. **Audit logging is independent** of accounting — a failed audit log
   never rolls back a committed quota mutation.
