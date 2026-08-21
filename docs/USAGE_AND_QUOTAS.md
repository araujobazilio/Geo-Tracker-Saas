# Usage and Quotas

## Status

**IMPLEMENTED** (Phase 3, integrated with the Phase 6 Scan Engine). Quota
enforcement for AI Checks is implemented via `QuotaService`, backed by
PostgreSQL row-level locking.

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

For a STANDARD scan, Phase 6 reserves the **entire** Cartesian plan
(`active prompts × eligible providers`) before any provider call. One
successfully and durably recorded PromptRun commits exactly one customer AI
Check. Provider-internal web searches/tool calls may increase provider cost but
do not create additional customer checks. Failed executions commit no event,
consume zero, and release their reserved balance at finalization. A valid answer
with no brand mention is still `SUCCEEDED`; execution failures are excluded from
future Phase 7 metric denominators. See `docs/SCAN_ENGINE.md`.

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

### Fixed period locking with `populate_existing()`

Period rows are locked via `get_for_period_for_update()` /
`get_by_id_for_update()`, which issue `SELECT ... FOR UPDATE` with
`populate_existing()`. This refreshes the ORM object in place with the
CURRENT row values after the lock is acquired, rather than loading the
ORM object normally and then running a separate `SELECT id FOR UPDATE`.
Without `populate_existing()`, quota math could operate on stale
pre-lock snapshots, defeating the purpose of the lock.

### Lock ordering

Lock ordering is documented and enforced to avoid deadlocks:

- **New reservations:** `WorkspaceUsagePeriod` (`FOR UPDATE`) → create
  `QuotaReservation`.
- **Existing reservation mutations** (`commit_ai_checks`,
  `release_reservation`): `QuotaReservation` (`FOR UPDATE`) →
  `WorkspaceUsagePeriod` (`FOR UPDATE`, via
  `reservation.usage_period_id`).

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
| `usage_period_id` | UUID FK → `workspace_usage_periods.id` (RESTRICT), NOT NULL | Permanently binds reservation to its originating period |
| `project_id` | UUID FK → `projects.id` (SET NULL), nullable | |
| `user_id` | UUID FK → `users.id` (SET NULL), nullable | |
| `idempotency_key` | String(255) | Unique — retry returns existing record |
| `ai_checks_reserved` | Integer | CHECK `> 0` (must reserve at least 1) |
| `ai_checks_committed` | Integer | CHECK `>= 0` and `<= ai_checks_reserved` |
| `status` | String(20) | `ACTIVE` / `COMMITTED` / `RELEASED` / `EXPIRED` |
| `expires_at` | DateTime(tz), nullable | TTL deadline for ACTIVE reservations |

Constraints:

- `uq_quota_reservations_idempotency_key` — unique idempotency key.
- `quota_reservations.usage_period_id` — `NOT NULL`, FK →
  `workspace_usage_periods.id` (`ON DELETE RESTRICT`). Permanently binds
  each reservation to the period where quota was originally reserved.
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

### Period binding and the cross-month invariant

Each reservation stores `usage_period_id`, a `NOT NULL` foreign key to
`workspace_usage_periods.id` (`ON DELETE RESTRICT`). This permanently
binds the reservation to the exact `WorkspaceUsagePeriod` where quota
was originally reserved.

The cross-month invariant: `commit_ai_checks()`,
`release_reservation()`, and `expire_stale_reservations()` always
update the **ORIGINAL** period referenced by
`reservation.usage_period_id`, never the current month
(`month_period(self._now)`). This prevents a reservation created late
in one month from debiting or crediting the next month's period when it
is committed, released, or expired after the month boundary.

## QuotaService methods

`QuotaService` (`app/services/quota_service.py`) controls its own
transaction boundaries for each mutation. Audit logging is independent
— accounting consistency never depends on audit success.

### `reserve_ai_checks(workspace_id, requested_checks, idempotency_key, ...)`

Atomically reserve AI Checks before a scan/provider call executes.

- Uses `SELECT ... FOR UPDATE` on the usage period row (via
  `get_for_period_for_update()` with `populate_existing()`).
- Creates the reservation with `usage_period_id` pointing to the exact
  period that was locked.
- Idempotent on `idempotency_key`: retrying with the same key returns
  the existing reservation. Reusing the same key with conflicting
  parameters raises `ConflictError`.
- **Re-check under lock:** re-checks the idempotency key AFTER
  acquiring the period lock. If a concurrent insert wins the race
  (`IntegrityError`), the session is rolled back and the existing
  reservation is returned.
- **Cross-workspace project validation:** validates that `project_id`
  belongs to `workspace_id` via `ProjectRepository.get_in_workspace()`.
  Rejects with `ConflictError` if the project is not in the workspace.
- Raises `QuotaExceededError` (429) if not enough quota remaining.
- Sets `expires_at = now + ttl_seconds` when explicitly supplied, otherwise
  `quota_reservation_ttl_seconds`. STANDARD scans supply
  `scan_reservation_ttl_seconds` (default 6 hours), reserving the whole plan.

### `commit_ai_checks(reservation_id, quantity, usage_idempotency_key, ...)`

Commit N AI Checks against a reservation after the provider call
succeeds. Atomically:

- Locks the reservation row with `get_by_id_for_update()` before
  mutation, then locks the originating usage period (via
  `reservation.usage_period_id`) with `get_by_id_for_update()`.
- Decrements `ai_checks_reserved`, increments `ai_checks_used` on the
  **ORIGINAL** usage period (never the current month).
- Increments `ai_checks_committed` on the reservation.
- If `committed == reserved`, marks the reservation `COMMITTED`.
- Creates an immutable `UsageEvent` linked via `quota_reservation_id`.
- The Scan Engine uses `quantity=1` and
  `prompt-run:{prompt_run_id}:usage`, composing this mutation into the same
  transaction as PromptRun evidence and ResponseSource retention. A rollback
  therefore commits neither evidence nor customer usage.

Idempotent on `usage_idempotency_key`: retrying returns the existing
`UsageEvent` without double-counting. **Re-check under lock:**
re-checks the usage idempotency key AFTER acquiring the reservation
lock. If a concurrent insert wins the race (`IntegrityError`), the
session is rolled back and the existing usage event is returned.
Raises `ConflictError` if the same key is reused with conflicting
parameters (different `reservation_id` or `quantity`), if the quantity
exceeds the remaining uncommitted balance, or the reservation is not
in an active/committed state.

### `release_reservation(reservation_id, *, commit_transaction=True)`

Release remaining uncommitted reserved checks back to the pool (e.g.
scan canceled or failed). Locks the reservation row with
`get_by_id_for_update()` before mutation, then locks the originating
usage period (via `reservation.usage_period_id`). Credits the
**ORIGINAL** period, never the current month. Idempotent: calling twice
does not subtract twice.

When `remaining > 0`, the reservation is set to `RELEASED` and the
period's `ai_checks_reserved` is decremented. When `remaining == 0`
(all reserved checks were committed), the reservation is set to
`COMMITTED` to preserve its fully-consumed history rather than marking
it `RELEASED`.

The `commit_transaction` parameter (Phase 6.1) controls transaction
ownership:

- `commit_transaction=True` (default): the method commits internally
  and records a `QUOTA_RELEASED` audit event in a separate session.
  Terminal reservation no-ops also commit to release locks.
- `commit_transaction=False`: the caller owns the surrounding
  transaction. The method locks, mutates, and flushes but does not
  commit or record audit. `ScanFinalizationService` uses this mode so
  the Scan terminal state, `Project.last_scan_at`, and quota release
  commit atomically in one transaction.

### `expire_stale_reservations()`

Expire `ACTIVE` reservations whose `expires_at` has passed. Uses
`FOR UPDATE SKIP LOCKED` (via
`list_expired_active_for_update_skip_locked()`) so multiple workers
never process the same reservation. Releases the remaining reserved
balance back to the **ORIGINAL** period (via
`reservation.usage_period_id`) and sets each to `EXPIRED`. Returns the
count of reservations expired. Intended to be called by a periodic
Celery Beat job.

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
| `quota_reservation_id` | UUID FK → `quota_reservations.id` (RESTRICT), nullable | Real FK linking to the originating reservation |

`idempotency_key` is unique when present (partial uniqueness enforced
via a `UNIQUE` constraint). A provider-call retry must not result in
double-counted AI Checks, tokens, or cost.

`quota_reservation_id` is a real foreign key to
`quota_reservations.id` with `ON DELETE RESTRICT` (added in the quota
period and concurrency integrity hardening migration). This
strengthens referential integrity while preserving accounting history:
reservation rows are never deleted, and the RESTRICT policy guarantees
a usage event can never be orphaned from its originating reservation.

## Transaction error handling

All `QuotaService` mutation operations roll back the session on
`QuotaExceededError`, `ConflictError`, or `IntegrityError`. This leaves
the session in a usable state with no held locks or partial mutations.
Callers can continue using the same session for subsequent operations
without encountering stale transaction state.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `quota_reservation_ttl_seconds` | `1800` (30 minutes) | Generic TTL after which an ACTIVE reservation is eligible for expiration |
| `scan_reservation_ttl_seconds` | `21600` (6 hours) | Explicit TTL used for the full STANDARD scan reservation |
| `scan_stale_after_seconds` | `7200` (2 hours) | Age after which RUNNING unresolved scan work is failed without provider retry |

## Confidence scan quota economics (Phase 8)

A `CONFIDENCE` scan repeats the same Prompt × Provider cells `repeat_count`
times, so the plan is larger than the baseline STANDARD scan:

```
planned_ai_checks = prompt_count × provider_count × repeat_count
```

### Full reservation before execution

As with STANDARD scans, **all** planned AI Checks are reserved before any
provider call is dispatched. Partial reservation is not allowed. If the
workspace cannot reserve the full `planned_ai_checks` amount, the CONFIDENCE
scan is rejected with `QuotaExceededError` and zero checks are consumed.

### Failed observation release

A failed observation (`FAILED` PromptRun) commits no `UsageEvent` and consumes
zero AI Checks, identical to STANDARD scan failure semantics. At finalization,
`ScanFinalizationService` releases all remaining uncommitted reservation
balance in one atomic transaction. This means only succeeded observations
consume customer quota; failed observations release their reserved checks back
to the pool.

### Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `confidence_scan_default_repeats` | `3` | Default `repeat_count` when a CONFIDENCE scan is created without an explicit value |
| `confidence_scan_max_repeats` | `5` | Maximum allowed `repeat_count`; higher values are rejected |

### Entitlement requirement

CONFIDENCE scans require the `confidence_scans` entitlement flag on the
workspace's effective plan. A workspace without this entitlement cannot create
CONFIDENCE scans; the creation endpoint returns a 403/entitlement error before
any quota is reserved.

## Verification scan quota economics (Phase 10)

A `VERIFICATION` scan is a single-repeat clone of the frozen
implementation baseline STANDARD scan. It re-measures the exact same
Prompt × Provider cells once (`repeat_count = 1`):

```
planned_ai_checks = prompt_count × provider_count × 1
```

This equals the baseline scan's `planned_ai_checks`. As with STANDARD
and CONFIDENCE scans, **all** planned AI Checks are reserved before
any provider call is dispatched. If the workspace cannot reserve the
full amount, the verification scan is rejected with
`QuotaExceededError` and zero checks are consumed.

### Evaluation is zero-cost

`VerificationEvaluationService.evaluate()` performs a deterministic
before/after comparison using only persisted evidence. It creates
**zero** UsageEvents, consumes **zero** AI Checks, and makes **zero**
provider calls. Only the scan execution costs AI Checks; the outcome
determination is free.

### Entitlement requirement

VERIFICATION scans require the `verification_scans` entitlement flag
on the workspace's effective plan. A workspace without this
entitlement cannot create verification scans; the creation endpoint
returns a 403/entitlement error before any quota is reserved.

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
   oversubscription. Period locks use `populate_existing()` to ensure
   quota math operates on CURRENT row values, not stale pre-lock
   snapshots.
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
8. **Reservations are permanently bound to their originating period**
   via `usage_period_id`. Commit/release/expire always update the
   ORIGINAL period, never the current month (cross-month invariant).
9. **Idempotency is re-checked under the lock** to handle concurrent
   races. `IntegrityError` from a winning concurrent insert is handled
   by rolling back and returning the existing record.
10. **Multi-worker expiration uses `FOR UPDATE SKIP LOCKED`** so
    multiple workers never process the same stale reservation.
11. **Cross-workspace project validation** rejects `project_id` values
    that do not belong to `workspace_id` with `ConflictError`.
12. **Transaction error handling** rolls back the session on
    `QuotaExceededError`, `ConflictError`, or `IntegrityError`, leaving
    it usable with no held locks or partial mutations.
13. **Full-plan reservation precedes dispatch** for STANDARD scans; success
    commits one check per PromptRun and finalization releases every failed or
    otherwise unused check.
14. **Stale recovery never retries a provider.** Celery worker-loss ambiguity is
    absorbed by GEO; unresolved work is failed and its customer quota released.
