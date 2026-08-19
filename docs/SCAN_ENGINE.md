# Scan Engine

## Status and scope

**IMPLEMENTED (Phase 6).** The Scan Engine executes reproducible `STANDARD` scans. A customer requests a scan; GEO Tracker fixes its plan before any provider call, reserves the entire customer AI Check budget, dispatches one Celery task, executes each planned `PromptRun` at most once, records evidence and accounting atomically, and classifies the scan from terminal run states.

Phase 6 does not perform brand/mention detection. That is Phase 7. A valid provider answer containing no tracked-brand mention is a successful measurement (`SUCCEEDED`), not an execution failure.

## STANDARD methodology

The policy is fixed by `ProviderExecutionPolicy`; the public API cannot override surfaces or modes.

| Provider | Surface | STANDARD mode |
|---|---|---|
| OpenAI | `OPENAI_RESPONSES_API` | `WEB_GROUNDED` |
| Anthropic | `ANTHROPIC_MESSAGES_API` | `WEB_GROUNDED` |
| Google | `GOOGLE_INTERACTIONS_API` | `MODEL_ONLY` |
| Perplexity | `PERPLEXITY_SONAR_API` | `WEB_GROUNDED` |

Google grounding remains disabled for compliance. Google results measure the Interactions API, not Google AI Overviews.

A scan uses the intersection of providers enabled on the project and providers allowed by the workspace's effective entitlements, in the stable order OpenAI, Anthropic, Google, Perplexity. Preflight also requires an ACTIVE project, a current ACTIVE `PromptSet` with the current generator key, at least one active prompt, configured model IDs, supported adapter capabilities, and—when `PRICING_REQUIRE_RULE_FOR_EXECUTION=true`—an effective exact price rule for every selected non-Perplexity target. Perplexity can execute without a local rule because it can report request cost.

## Immutable execution snapshot

Creation stores the exact `PromptSet.id` on `Scan` and creates the full Cartesian plan:

```text
active Prompts in the selected PromptSet × eligible provider targets = PromptRuns
```

Each `PromptRun` snapshots:

- the exact `prompt_id` (whose immutable historical `Prompt.text`, country, and language are later sent);
- provider and provider surface;
- execution mode;
- requested model ID;
- `attempt_number = 1`.

`Scan.prompt_count`, `provider_count`, and `planned_ai_checks` snapshot the plan dimensions. Later project-provider, plan-entitlement, active PromptSet, or environment model changes do not mutate an already-created `Scan` or its `PromptRun` rows. `returned_model` is recorded separately when the provider reports it.

## Lifecycle

### 1. Create and preflight

`POST /api/v1/workspaces/{workspace_id}/projects/{project_id}/scans` requires ADMIN or OWNER access and an `Idempotency-Key`. The key is unique within a workspace. Reuse for the same project/type returns the existing scan and may resume dispatch if reservation succeeded but dispatch did not; conflicting reuse is rejected.

The service first persists the `PENDING` `Scan` and every `PENDING` `PromptRun`. No provider call has occurred.

### 2. Reserve the complete plan

Before dispatch, GEO Tracker reserves exactly `planned_ai_checks` against the workspace's UTC-month quota, using `scan:{scan_id}` as the reservation idempotency key. Partial reservation is not allowed. The scan-specific TTL is `SCAN_RESERVATION_TTL_SECONDS` (default **21,600 seconds / 6 hours**), not the generic 30-minute quota default.

If full reservation is impossible, all unresolved runs and the scan become `FAILED` with quota failure metadata. No provider call is dispatched and zero AI Checks are consumed.

### 3. Dispatch and duplicate-delivery claim

Celery transports only the scan UUID; PostgreSQL is authoritative. A worker locks the `Scan` row and changes `PENDING → RUNNING` before work. Any duplicate delivery that sees a non-`PENDING` row exits without issuing provider calls. Each `PromptRun` is also row-locked and claimed only from `PENDING → RUNNING`, providing a second duplicate-execution guard.

Celery uses **early acknowledgement** (`task_acks_late=False`), result storage is disabled, and the task has no `autoretry_for` and never calls `self.retry`. Provider adapters also perform one HTTP request per `execute()`. This intentionally avoids automatic repetition of paid calls.

A process can still die after a provider accepted a request but before evidence committed. That outcome is intrinsically ambiguous. GEO Tracker absorbs it as failed work rather than risking another customer-billed provider call.

### 4. Bounded execution

One task executes the snapshotted run IDs with an asyncio semaphore bounded by `SCAN_MAX_CONCURRENCY` (default **4**). Each run opens its own short-lived SQLAlchemy session for claim, result recording, or failure recording; a session is never shared across concurrent coroutines.

The adapter receives exactly the stored prompt text and snapshotted mode/model. There are no hidden system prompts and no execution retry.

### 5. Atomic successful result

A returned result must match the run's provider, surface, mode, and requested model snapshot. In one database transaction GEO Tracker:

1. resolves/calculates cost;
2. commits exactly **one** AI Check and creates one immutable `UsageEvent`;
3. stores normalized response evidence and usage on `PromptRun`;
4. stores ordered `ResponseSource` rows supplied by the provider;
5. marks the run `SUCCEEDED`.

The usage idempotency key is `prompt-run:{prompt_run_id}:usage`, and each run has at most one linked usage event. If any accounting/evidence write fails, the transaction rolls back and the run becomes `FAILED`; it does not consume an AI Check.

Provider-internal web searches and tool calls can increase provider cost and are recorded as `search_requests` when reported. They do **not** multiply customer quota: one successfully and durably recorded `PromptRun` is exactly one customer AI Check.

### 6. Failure and finalization

Provider, malformed-response, accounting, and internal execution failures mark that run `FAILED`; they commit no usage event and consume zero AI Checks. After all runs are terminal:

| Successful runs | Scan status |
|---|---|
| all planned runs | `COMPLETED` |
| at least one, but not all | `PARTIAL` |
| zero | `FAILED` |

Every successful run has already committed one check. Finalization releases all remaining uncommitted reservation balance, so failures consume zero. Failed runs are execution failures, not measurements, and Phase 7 must exclude them from future metric denominators. Conversely, a non-empty, valid answer with no brand mention is `SUCCEEDED` and belongs in those denominators.

#### Atomic finalization (Phase 6.1)

`ScanFinalizationService.finalize()` commits the Scan's terminal state, `Project.last_scan_at`, and the unused quota release in **one** transaction. If quota release fails, the Scan does not become terminal — the entire finalization rolls back, leaving the Scan `RUNNING` for retry. This guarantees a terminal Scan never strands an active reservation with unused reserved checks.

`QuotaService.release_reservation()` accepts a `commit_transaction` parameter. The finalizer calls it with `commit_transaction=False` so the caller owns the single atomic commit. When all reserved checks were committed (no remaining balance), the reservation is marked `COMMITTED` rather than `RELEASED` to preserve its fully-consumed history.

#### Idempotent reconciliation

Calling `finalize()` on an already-terminal Scan is safe. The finalizer detects the terminal state and reconciles any stranded `ACTIVE` reservation idempotently — no provider calls, no new `UsageEvent`s. This self-heals legacy or inconsistent terminal scans that predate atomic finalization.

#### Run-count invariant

Before classifying a Scan, the finalizer verifies that the `PromptRun` row count matches the immutable Scan plan (`planned_ai_checks`) and that `succeeded + failed == planned_ai_checks`. A mismatch indicates internal data corruption and causes finalization to roll back with `InfrastructureError`, leaving the Scan non-terminal for operator investigation.

#### Pre-execution rejection

When `_claim_scan` finds the reservation missing or not `ACTIVE`/`COMMITTED` before any provider call, it marks every `PromptRun` `FAILED` with `ACCOUNTING_ERROR`, records the rejection reason on the Scan, and then finalizes atomically. No provider is invoked. This guarantees the invariant **terminal Scan → zero unresolved PromptRuns** even on rejection.

## Worker loss and stale recovery

`ScanRecoveryService` finds both `RUNNING` and `PENDING` scans older than `SCAN_STALE_AFTER_SECONDS` (default **7,200 seconds / 2 hours**). It marks unresolved `PENDING`/`RUNNING` runs `FAILED` with an internal-error explanation, then uses normal finalization to classify the scan and release unused reservation quota.

**Recovery never calls or retries a provider.** This is deliberate: stale `RUNNING` may mean a response was lost after the provider charged GEO Tracker. Avoiding a duplicate paid request takes precedence over attempting to recover the measurement. Stale `PENDING` scans were either never dispatched (broker/task lost under early acknowledgement) or dispatched but never claimed by a worker — there is no provider request to replay.

The scan reservation TTL is intentionally longer than the stale threshold by default. Operators must run stale-scan recovery before reservation expiry cleanup can make an in-flight scan's reservation invalid.

## Evidence and retention

Successful `PromptRun` evidence retains final response text; requested and returned model IDs; request/support and response/object IDs; finish reason; latency; search flag; normalized token/search usage; internal cost fields; completion timestamps; and provider-returned citations.

`ResponseSource` preserves provider order (`ordinal`) and the returned URL, title, source type, text offsets, and cited text when available. GEO Tracker does **not** fetch source URLs, resolve redirects, or invent/canonicalize citations during Phase 6. Reasoning token counts may be retained for accounting, but reasoning/thinking content is discarded.

Foreign keys use `RESTRICT` for scans, prompt runs, sources, usage, pricing evidence, and quota history so evidence cannot be silently cascade-deleted.

## Customer API boundary

Tenant-scoped read endpoints expose scan status, run result/evidence, and sources. Customer schemas deliberately omit token counts, `cost_usd`, provider-reported/calculated costs, `CostSource`, pricing-rule IDs, usage-event IDs, and quota-reservation IDs. Cost accounting is an internal operator/financial concern; it is not returned by the customer Scan API.

See also [Cost Accounting](COST_ACCOUNTING.md), [Usage and Quotas](USAGE_AND_QUOTAS.md), and [Provider Integrations](PROVIDER_INTEGRATIONS.md).
