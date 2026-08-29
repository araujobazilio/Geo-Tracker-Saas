# Verification Scans and Opportunity Outcome Tracking (Phase 10 + 10.1 + 10.3)

## Overview

Phase 10 introduces **Verification Scans** and **Opportunity Outcome
Tracking** — a deterministic, zero-AI-Check before/after comparison that
measures whether a user's implementation work resolved a previously
detected Action Center Opportunity.

### Key Principles

- **Zero AI Checks for evaluation**: the deterministic comparison uses
  only persisted evidence. Creating the verification scan costs AI
  Checks (it repeats the baseline's measurement cells); evaluating the
  result costs zero.
- **No causal claims**: `VERIFIED` does NOT prove the user's
  implementation caused the improvement. It means the originally
  measured issue is no longer present under the verification
  methodology.
- **Methodology cloning**: the verification scan clones the frozen
  implementation baseline's exact prompts, providers, surfaces,
  execution modes, requested models, and entity snapshots. Current
  project configuration cannot alter the cloned methodology.
- **Deterministic outcomes**: the `VerificationOutcome` is computed by
  fixed rules, not by an LLM. The same evidence always produces the
  same outcome.
- **Historical fidelity**: the frozen baseline occurrence + baseline
  scan are immutable. Post-implementation configuration changes
  cannot rewrite the baseline.

## Lifecycle

```
OPEN → IN_PROGRESS → IMPLEMENTED → [Verification Scan] → VERIFIED
                         ↑              |                  |
                         |              |                  | RESOLVED only
                         +--------------+                  |
                         (clear baseline)                  read-only
```

1. **User marks Opportunity IMPLEMENTED**: the latest eligible
   `OpportunityOccurrence` is frozen as the
   `implementation_baseline_occurrence_id`. The baseline scan must be
   STANDARD, terminal (COMPLETED/PARTIAL), with a COMPLETED
   `ScanAnalysis`.

2. **User creates a Verification Scan**: `POST
   /api/v1/workspaces/{wid}/projects/{pid}/opportunities/{oid}/verification`.
   Clones the baseline's methodology, reserves quota, and dispatches.
   Requires `verification_scans` entitlement.

3. **Verification Scan executes**: repeats the baseline's exact
   targeted measurement cells once (repeat_count=1). Same bounded
   async execution as STANDARD scans.

4. **Automatic evaluation (Phase 10.1)**: after the verification scan
   is finalized and its analysis completes, the
   `VerificationEvaluationService` is automatically triggered. The
   evaluation computes the verification metrics via
   `CompetitorExplanationService`, compares to the frozen baseline,
   and persists the `VerificationOutcome`. Zero AI Checks. If
   automatic evaluation fails, the verification remains PENDING and
   can be evaluated manually.

5. **Manual evaluation (fallback)**: `POST
   /api/v1/workspaces/{wid}/projects/{pid}/opportunities/{oid}/verifications/{vid}/evaluate`.
   Can be called if automatic evaluation failed or was not triggered.
   Idempotent — re-calling on an already-evaluated verification
   returns the existing result.

6. **If RESOLVED**: the parent Opportunity transitions to `VERIFIED`
   via the system-only `mark_verified_from_verification()` method.
   `VERIFIED` is read-only — no further transitions are allowed.

## VerificationOutcome

| Outcome | Meaning |
|---------|---------|
| `PENDING` | Verification exists but evaluation has not completed. |
| `RESOLVED` | The issue falls below its Action Engine trigger threshold and passes verification quality gates. |
| `IMPROVED` | The issue materially improved but is still above its resolution threshold. |
| `NOT_IMPROVED` | Change is smaller than the configured meaningful-improvement threshold. |
| `REGRESSED` | The measured issue materially worsened. |
| `INCONCLUSIVE` | The new measurement cannot support a reliable comparison (coverage, analysis, missing eligible evidence, etc.). |

`SUCCESS` and `FAILURE` are intentionally NOT included — they imply a
stronger causal interpretation than the deterministic before/after
comparison supports.

## VerificationReasonCode (INCONCLUSIVE only)

| Code | Meaning |
|------|---------|
| `INSUFFICIENT_COVERAGE` | Verification measurement coverage < 75%. |
| `INSUFFICIENT_BASELINE_COVERAGE` | Baseline measurement coverage < 75% (Phase 10.1). |
| `ANALYSIS_NOT_COMPLETED` | Verification scan analysis is not COMPLETED. |
| `NO_SUCCESSFUL_OBSERVATIONS` | Baseline or verification scan has zero successful observations. |
| `INSUFFICIENT_CITATION_EVIDENCE` | Citation-eligible observations < MIN_CITATION_ELIGIBLE_OBSERVATIONS (baseline or verification). |
| `BASELINE_EVIDENCE_UNAVAILABLE` | Frozen baseline occurrence or scan not found. |

## Decision Logic

### Coverage Gates (applied before metric comparison)

1. Verification scan analysis must be COMPLETED → else INCONCLUSIVE
   (`ANALYSIS_NOT_COMPLETED`).
2. Verification measurement coverage ≥ 75% → else INCONCLUSIVE
   (`INSUFFICIENT_COVERAGE`).
3. Verification scan must have ≥ 1 successful observation → else
   INCONCLUSIVE (`NO_SUCCESSFUL_OBSERVATIONS`).
4. Baseline scan must have ≥ 1 successful observation → else
   INCONCLUSIVE (`NO_SUCCESSFUL_OBSERVATIONS`). (Phase 10.1)
5. Baseline measurement coverage ≥ 75% → else INCONCLUSIVE
   (`INSUFFICIENT_BASELINE_COVERAGE`). (Phase 10.1)
6. For `OWNED_CITATION_GAP`: BOTH baseline AND verification
   citation-eligible observations ≥ `MIN_CITATION_ELIGIBLE_OBSERVATIONS`
   → else INCONCLUSIVE (`INSUFFICIENT_CITATION_EVIDENCE`). (Phase 10.1)

### Brand-Side Resolution Safeguards (Phase 10.1)

A gap closing to 0 because the **brand disappeared** (not because the
issue was resolved) is NOT a resolution. The following safeguards
prevent false RESOLVED outcomes:

- `DISCOVERY_VISIBILITY_GAP` / `PROVIDER_VISIBILITY_GAP`: brand
  visibility rate must be > 0 in the verification scan.
- `OWNED_CITATION_GAP`: brand owned citation rate must be > 0.
- `PROMPT_COMPETITOR_GAP`: brand must appear in at least one
  verification observation.

If the safeguard fails, the outcome is `NOT_IMPROVED` with a message
explaining that the gap closed because the brand disappeared.

### Metric Comparison

| Opportunity Type | Metric | Resolution Threshold | Meaningful Improvement |
|-----------------|--------|---------------------|----------------------|
| `DISCOVERY_VISIBILITY_GAP` | `visibility_gap_pp` | ≥ 10pp | ≥ 5pp |
| `PROVIDER_VISIBILITY_GAP` | `visibility_gap_pp` | ≥ 15pp | ≥ 5pp |
| `OWNED_CITATION_GAP` | `citation_gap_pp` | ≥ 20pp | ≥ 5pp |
| `PROMPT_COMPETITOR_GAP` | `competitor_only_rate` | == 0% | ≥ 10pp |

**Phase 10.1 change**: `PROMPT_COMPETITOR_GAP` now uses
`competitor_only_rate` (percentage points) instead of
`competitor_only_count` (integer count), making the comparison
consistent with other percentage-point metrics.

**Decision tree** (after coverage gates + brand safeguards pass):

1. If `verification_value < resolution_threshold` AND brand safeguard
   passes → **RESOLVED**
2. Else if `delta = baseline - verification ≥ improvement_threshold` → **IMPROVED**
3. Else if `-delta ≥ improvement_threshold` → **REGRESSED**
4. Else → **NOT_IMPROVED**

For `PROMPT_COMPETITOR_GAP`: `RESOLVED` when `competitor_only_rate == 0`
AND the brand appears in the verification scan.

## Methodology Version

```
VERIFICATION_METHODOLOGY_VERSION = "opportunity-verification-v1"
```

Future methodology changes require a version bump.

## Entitlements

- `verification_scans_enabled` on the plan controls access.
- `require_feature(workspace_id, "verification_scans")` is called
  during verification scan creation.
- Phase 10.1: only providers in the **selected scope** must be allowed
  by the current plan and enabled for the project. An unrelated
  baseline provider that is currently unavailable does NOT block a
  provider-specific verification.

## API Endpoints

| Method | Path | Role | Description |
|--------|------|------|-------------|
| `POST` | `/opportunities/{oid}/verification` | ADMIN | Create + dispatch verification scan. |
| `GET` | `/opportunities/{oid}/verifications` | MEMBER | List verification records (newest first). |
| `GET` | `/opportunities/{oid}/verifications/{vid}` | MEMBER | Get single verification record. |
| `POST` | `/opportunities/{oid}/verifications/{vid}/evaluate` | ADMIN | Trigger deterministic evaluation. |
| `GET` | `/opportunities/{oid}/verification-summary` | MEMBER | Summary of verification outcomes. |

All endpoints enforce tenant isolation. Cross-tenant access returns 404.

## Database Schema

### `opportunities.implementation_baseline_occurrence_id`

Nullable UUID FK to `opportunity_occurrences.id` (ON DELETE RESTRICT).
Frozen when an Opportunity transitions to IMPLEMENTED; cleared when
returning to IN_PROGRESS.

### `opportunity_verifications` (new table)

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID PK | |
| `workspace_id` | UUID FK | Tenant isolation. |
| `project_id` | UUID FK | |
| `opportunity_id` | UUID FK | |
| `baseline_occurrence_id` | UUID FK | Frozen baseline occurrence. |
| `baseline_scan_id` | UUID FK | Baseline STANDARD scan. |
| `verification_scan_id` | UUID FK (UNIQUE) | Verification scan. |
| `idempotency_key` | String(255) | Unique per workspace. |
| `verification_methodology_version` | String(50) | |
| `outcome` | String(20) | `VerificationOutcome` enum. |
| `reason_code` | String(50) | `VerificationReasonCode` (INCONCLUSIVE only). |
| `evaluation_message` | String(1000) | Human-readable result. |
| `metric_name` | String(100) | Compared metric. |
| `baseline_value` | Numeric(10,4) | |
| `verification_value` | Numeric(10,4) | |
| `delta_value` | Numeric(10,4) | baseline - verification. |
| `baseline_brand_value` | Numeric(10,4) | Brand-side metric for transparency. |
| `verification_brand_value` | Numeric(10,4) | |
| `baseline_coverage` | Numeric(10,4) | |
| `verification_coverage` | Numeric(10,4) | |
| `resolution_threshold` | Numeric(10,4) | |
| `meaningful_improvement_threshold` | Numeric(10,4) | |
| `evaluated_at` | DateTime | |
| `created_at` / `updated_at` | DateTime | Timestamps. |

**Unique constraints**:
- `(workspace_id, idempotency_key)`
- `verification_scan_id` (one scan per verification)

**Indexes**: `workspace_id`, `project_id`, `opportunity_id`, `outcome`,
`(opportunity_id, created_at)`.

## Status Transition Rules

| From | To | Allowed? | Notes |
|------|----|---------|-------|
| OPEN | IN_PROGRESS | ✓ | |
| OPEN | DISMISSED | ✓ | |
| IN_PROGRESS | OPEN | ✓ | |
| IN_PROGRESS | IMPLEMENTED | ✓ | Freezes baseline occurrence. |
| IN_PROGRESS | DISMISSED | ✓ | |
| IMPLEMENTED | IN_PROGRESS | ✓ | Clears frozen baseline. |
| IMPLEMENTED | DISMISSED | ✓ | |
| IMPLEMENTED | VERIFIED | ✓ (system only) | Only on RESOLVED outcome. |
| DISMISSED | OPEN | ✓ | |
| VERIFIED | * | ✗ | Read-only. |
| * | VERIFIED | ✗ (manual) | Reserved for system transition. |

## Quota

- Phase 10.1: verification scan creation reserves `planned_ai_checks`
  (= number of exact target cells) AI Checks (repeat_count=1). This
  may be less than `prompt_count × provider_count` for non-rectangular
  scopes (e.g., PROVIDER_VISIBILITY_GAP selects only one provider's
  cells).
- Evaluation costs zero AI Checks.
- Quota failure marks the scan as FAILED with `QUOTA_EXCEEDED`.

## Concurrency

- `OpportunityWorkflowService.transition()` acquires a row-level lock
  on the Opportunity (`SELECT ... FOR UPDATE`).
- `VerificationEvaluationService.evaluate()` acquires a row-level lock
  on the `OpportunityVerification` record.
- `mark_verified_from_verification()` (Phase 10.1) independently
  validates that the verification record exists, belongs to this
  Opportunity, and has outcome RESOLVED before transitioning. It
  acquires a row-level lock on the Opportunity and checks that it is
  still IMPLEMENTED. If the user changed the status while the
  verification was in flight, the result is preserved as historical
  evidence but the Opportunity is NOT forced to VERIFIED.

## Idempotency

- Verification scan creation is idempotent via
  `(workspace_id, idempotency_key)`.
- Phase 10.1: reusing the same idempotency key after a re-implementation
  cycle (different baseline occurrence) raises `ConflictError`.
- Re-calling `evaluate()` on an already-evaluated verification returns
  the existing result without re-computing.

## One Pending Verification Per Cycle (Phase 10.1)

- At most one PENDING verification may exist per implementation cycle
  (opportunity + baseline occurrence).
- The service-level check in `VerificationScanCreationService` blocks
  creation of a second PENDING verification.
- A partial unique index on `opportunity_verifications(opportunity_id,
  baseline_occurrence_id) WHERE outcome = 'PENDING'` enforces this at
  the database level.
- After a verification is evaluated (terminal outcome), a new
  verification can be created for the same cycle.

## Targeted Scope (Phase 10.1)

The `VerificationScopeResolver` determines the exact historical
baseline cells to re-measure based on the Opportunity type:

| Opportunity Type | Scope Rule |
|-----------------|------------|
| `DISCOVERY_VISIBILITY_GAP` | All NON_BRANDED cells across all providers. |
| `PROVIDER_VISIBILITY_GAP` | NON_BRANDED cells for the Opportunity's provider only. |
| `OWNED_CITATION_GAP` | NON_BRANDED cells with WEB_GROUNDED execution mode only. |
| `PROMPT_COMPETITOR_GAP` | Exact prompt_id cells across all providers. |

The same scope drives BOTH the provider execution plan AND the
baseline/verification evaluation, ensuring that the comparison is
performed on corresponding methodological cells.

`planned_ai_checks = len(target_cells)`, NOT
`prompt_count × provider_count`. This correctly handles
non-rectangular plans where not all prompts run on all providers.

## Automatic Evaluation (Phase 10.1)

After a VERIFICATION scan is finalized and its `ScanAnalysis` completes
successfully, the `VerificationEvaluationService.evaluate()` is
automatically triggered by the `ScanFinalizationService`.

- Evaluation runs in the same session as the analysis.
- Evaluation failure is logged and swallowed — it MUST NOT rollback
  the analysis, replay providers, or change quota. The verification
  remains PENDING and can be evaluated manually.
- The scan remains terminal regardless of evaluation outcome.

## Auto-Terminalization (Phase 10.3)

The `ScanFinalizationService` now guarantees that a `PENDING`
`OpportunityVerification` is never left stranded after its scan
reaches a terminal state. A centralized post-finalization helper
handles all terminal paths:

### FAILED Verification Scan (all runs failed)

When a `VERIFICATION` scan's `ScanStatus` is `FAILED` (all `PromptRuns`
failed), the finalization service automatically terminalizes the
associated `OpportunityVerification` as:

- **Outcome**: `INCONCLUSIVE`
- **Reason Code**: `VERIFICATION_SCAN_FAILED`
- **evaluated_at**: set to the finalization timestamp

No manual lifecycle call is needed. The opportunity remains
`IMPLEMENTED`.

### Analysis Exception (durable `ScanAnalysis=FAILED`)

When `ScanAnalysisService.analyze()` raises an unexpected exception
after persisting `ScanAnalysis=FAILED` in its failure transaction, the
post-finalization helper detects the durable `FAILED` analysis state
and terminalizes the `OpportunityVerification` as:

- **Outcome**: `INCONCLUSIVE`
- **Reason Code**: `ANALYSIS_NOT_COMPLETED`
- **evaluated_at**: set to the reconciliation timestamp

No provider replay occurs. The scan remains `COMPLETED`/`PARTIAL`.

### Evaluation Exception (ephemeral)

When `VerificationEvaluationService.evaluate()` raises an unexpected
exception after `ScanAnalysis=COMPLETED`, the verification remains
`PENDING` for local retry. This is an ephemeral error — the analysis
is durable and the scan is terminal. Manual evaluation can be called
to complete the evaluation without provider replay.

### Idempotent Self-Healing

Re-calling `finalize()` on an already-terminal `FAILED` verification
scan reconciles any stranded `PENDING` verification. This makes the
finalization service idempotent and self-healing — operators can
safely re-run finalization to repair inconsistent state.

### `VERIFICATION_SCAN_FAILED` Reason Code (Phase 10.3)

| Code | Meaning |
|------|---------|
| `VERIFICATION_SCAN_FAILED` | The verification scan itself failed (all runs failed or quota exceeded). |

This is distinct from `ANALYSIS_NOT_COMPLETED` (the scan completed but
analysis failed) and `INSUFFICIENT_COVERAGE` (the scan and analysis
succeeded but coverage was too low).

### Stale Recovery Lifecycle Closure (Phase 10.4)

`ScanRecoveryService.recover_stale_scans()` previously finalized stale
scans with `trigger_analysis=False`, which left the
`OpportunityVerification` stranded `PENDING` for stale `VERIFICATION`
scans recovered to `FAILED` or `PARTIAL`.

Phase 10.4 closes this gap: after recovering a stale `VERIFICATION`
scan, `ScanRecoveryService` explicitly calls
`ScanFinalizationService.reconcile_verification_lifecycle(scan_id)`.

**FAILED recovery** (all runs unresolved):

- Scan → `FAILED`
- Verification → `INCONCLUSIVE` / `VERIFICATION_SCAN_FAILED`
- Opportunity remains `IMPLEMENTED`
- Zero provider replay, zero new AI Checks, zero new UsageEvents
- PENDING partial-unique slot released for a new attempt

**PARTIAL recovery** (some runs already succeeded durably):

- Unresolved runs → `FAILED`, successful runs unchanged
- Scan → `PARTIAL`
- Deterministic analysis + evaluation run locally using only the
  persisted successful observations
- Outcome may be `INCONCLUSIVE`, `IMPROVED`, `NOT_IMPROVED`,
  `REGRESSED`, or `RESOLVED` per existing methodology
- Low-coverage recovered scans naturally become `INCONCLUSIVE` /
  `INSUFFICIENT_COVERAGE`
- Zero provider replay, zero new UsageEvents

**Analysis failure during recovery**: reuses Phase 10.3
durable-analysis reconciliation — verification terminalized as
`INCONCLUSIVE` / `ANALYSIS_NOT_COMPLETED`.

**Ephemeral evaluation error during recovery**: if analysis `COMPLETED`
but evaluation raises a transient error, the verification may remain
`PENDING` for manual retry — same rule as the normal production path.

**Idempotency**: recovering the same stale scan twice produces the same
terminal outcome. No duplicate analysis, no duplicate verification, no
provider calls.

**Retry after recovered FAILED**: a new verification with a different
idempotency key is allowed — the previous `INCONCLUSIVE` slot does not
block it.
