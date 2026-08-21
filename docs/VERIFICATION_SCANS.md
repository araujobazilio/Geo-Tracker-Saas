# Verification Scans and Opportunity Outcome Tracking (Phase 10)

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
   Prompt × Provider measurement cells once (repeat_count=1). Same
   bounded async execution as STANDARD scans.

4. **User triggers evaluation**: `POST
   /api/v1/workspaces/{wid}/projects/{pid}/opportunities/{oid}/verifications/{vid}/evaluate`.
   Computes the verification metrics via
   `CompetitorExplanationService`, compares to the frozen baseline,
   and persists the `VerificationOutcome`. Zero AI Checks.

5. **If RESOLVED**: the parent Opportunity transitions to `VERIFIED`
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
| `ANALYSIS_NOT_COMPLETED` | Verification scan analysis is not COMPLETED. |
| `NO_SUCCESSFUL_OBSERVATIONS` | Verification scan has zero successful observations. |
| `INSUFFICIENT_CITATION_EVIDENCE` | Citation-eligible observations < MIN_CITATION_ELIGIBLE_OBSERVATIONS. |
| `BASELINE_EVIDENCE_UNAVAILABLE` | Frozen baseline occurrence or scan not found. |

## Decision Logic

### Coverage Gates (applied before metric comparison)

1. Verification scan analysis must be COMPLETED → else INCONCLUSIVE
   (`ANALYSIS_NOT_COMPLETED`).
2. Verification measurement coverage ≥ 75% → else INCONCLUSIVE
   (`INSUFFICIENT_COVERAGE`).
3. Verification scan must have ≥ 1 successful observation → else
   INCONCLUSIVE (`NO_SUCCESSFUL_OBSERVATIONS`).
4. For `OWNED_CITATION_GAP`: citation-eligible observations ≥
   `MIN_CITATION_ELIGIBLE_OBSERVATIONS` → else INCONCLUSIVE
   (`INSUFFICIENT_CITATION_EVIDENCE`).

### Metric Comparison

| Opportunity Type | Metric | Resolution Threshold | Meaningful Improvement |
|-----------------|--------|---------------------|----------------------|
| `DISCOVERY_VISIBILITY_GAP` | `visibility_gap_pp` | ≥ 10pp | ≥ 5pp |
| `PROVIDER_VISIBILITY_GAP` | `visibility_gap_pp` | ≥ 15pp | ≥ 5pp |
| `OWNED_CITATION_GAP` | `citation_gap_pp` | ≥ 20pp | ≥ 5pp |
| `PROMPT_COMPETITOR_GAP` | `competitor_only_count` | == 0 | ≥ 10 |

**Decision tree** (after coverage gates pass):

1. If `verification_value < resolution_threshold` → **RESOLVED**
2. Else if `delta = baseline - verification ≥ improvement_threshold` → **IMPROVED**
3. Else if `-delta ≥ improvement_threshold` → **REGRESSED**
4. Else → **NOT_IMPROVED**

For `PROMPT_COMPETITOR_GAP`: `RESOLVED` when `competitor_only_count == 0`.

## Methodology Version

```
VERIFICATION_METHODOLOGY_VERSION = "opportunity-verification-v1"
```

Future methodology changes require a version bump.

## Entitlements

- `verification_scans_enabled` on the plan controls access.
- `require_feature(workspace_id, "verification_scans")` is called
  during verification scan creation.
- All baseline providers must still be allowed by the current plan and
  enabled for the project.

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

- Verification scan creation reserves `prompt_count × provider_count`
  AI Checks (repeat_count=1).
- Evaluation costs zero AI Checks.
- Quota failure marks the scan as FAILED with `QUOTA_EXCEEDED`.

## Concurrency

- `OpportunityWorkflowService.transition()` acquires a row-level lock
  on the Opportunity (`SELECT ... FOR UPDATE`).
- `VerificationEvaluationService.evaluate()` acquires a row-level lock
  on the `OpportunityVerification` record.
- `mark_verified_from_verification()` acquires a row-level lock on the
  Opportunity and checks that it is still IMPLEMENTED before
  transitioning. If the user changed the status while the verification
  was in flight, the result is preserved as historical evidence but
  the Opportunity is NOT forced to VERIFIED.

## Idempotency

- Verification scan creation is idempotent via
  `(workspace_id, idempotency_key)`.
- Re-calling `evaluate()` on an already-evaluated verification returns
  the existing result without re-computing.
