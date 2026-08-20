# Confidence Scans (Phase 8)

## Overview

A Confidence Scan repeats the SAME immutable measurement cells multiple
times so GEO Tracker can show how stable or variable the observed
visibility is.

It does NOT provide classical statistical confidence intervals.

## Core Concept

```
baseline STANDARD Scan
        |
        v
same PromptSet
same Prompt IDs
same provider surfaces
same execution modes
same requested models
same entity snapshots
        |
        v
N repeated observations per Prompt x Provider cell
        |
        v
deterministic Phase 7 analysis
        |
        v
measurement coverage
mention stability
visibility-by-round
visibility range
measurement confidence level
```

## What a Confidence Scan IS

Multiple planned independent observations of the same stored
Prompt x Provider measurement cells.

## What a Confidence Scan is NOT

- A retry
- A provider-error retry
- Another randomly regenerated PromptSet
- A Confidence Interval
- A statistical proof

## Baseline Requirements

A Confidence Scan is created from an existing STANDARD Scan (the
"baseline"). The baseline must:

- Belong to the same workspace/project
- Be `ScanType.STANDARD`
- Be `COMPLETED` or `PARTIAL`
- Have `successful_runs > 0`
- Have zero unresolved PromptRuns
- Have `ScanEntitySnapshot` rows
- Have its full immutable PromptRun plan

Rejected as baseline:
- `PENDING`
- `RUNNING`
- `FAILED` with zero valid observations
- `CONFIDENCE`
- `VERIFICATION`

## Baseline May Use Superseded PromptSet

A Confidence Scan repeats a historical baseline. The baseline PromptSet
does NOT need to be the current ACTIVE PromptSet. The baseline's exact
`prompt_set_id`, prompt IDs, provider targets, and entity snapshots are
used. This ensures historical reproducibility.

## Current Authorization / Commercial Rules

At Confidence Scan creation:

- Current Project must still be `ACTIVE`
- Current plan must have `confidence_scans_enabled == true`
- All baseline providers must still be:
  - Allowed by current entitlements
  - Enabled for the project
  - Server-configured

If any baseline provider is no longer allowed/configured, the entire
Confidence Scan is rejected before quota reservation. We do NOT silently
remove a baseline provider.

## Repeat Count

- Default: `CONFIDENCE_SCAN_DEFAULT_REPEATS=3`
- Max: `CONFIDENCE_SCAN_MAX_REPEATS=5`
- Validation: `2 <= repeat_count <= max`
- Absolute upper bound: `max <= 10`

## Schema Changes

### `Scan.repeat_count`

Positive integer. `STANDARD=1`, `CONFIDENCE>=2`.

### `Scan.baseline_scan_id`

UUID nullable, self-referencing FK to `scans.id` with `ON DELETE RESTRICT`.
`STANDARD=NULL`, `CONFIDENCE=baseline scan ID`.

### `PromptRun.observation_index`

Positive integer. Identifies which repeat observation this run belongs to.
`STANDARD=1`, `CONFIDENCE=1..repeat_count`.

### `attempt_number` vs `observation_index`

- `attempt_number`: reserved for actual execution attempts/retries.
- `observation_index`: identifies a planned repeat observation.

For all Phase 8 planned runs: `attempt_number = 1`.

### Unique Constraint

Old: `UNIQUE(scan_id, prompt_id, provider, attempt_number)`
New: `UNIQUE(scan_id, prompt_id, provider, observation_index, attempt_number)`

## Planned AI Check Formula

- `STANDARD`: `prompt_count x provider_count x 1`
- `CONFIDENCE`: `prompt_count x provider_count x repeat_count`

Example: 25 prompts x 4 providers x 3 observations = 300 planned AI Checks.

All planned checks are reserved before provider execution. Unused
reserved checks from failed observations are released at finalization.

## Execution: Round-by-Round

For CONFIDENCE scans, runs are executed round-by-round:

1. Execute all `observation_index=1` runs with bounded concurrency
2. Wait until round 1 finishes
3. Execute all `observation_index=2` runs
4. Wait until round 2 finishes
5. Continue for all rounds

This reduces accidental burst correlation and prevents sending the same
Prompt x Provider multiple times simultaneously.

STANDARD scans remain single-round (observation_index=1).

## No Automatic Retries

One PromptRun execution = at most one provider HTTP request. A failed
repeat is a FAILED observation. It contributes to coverage loss. It is
NOT automatically rerun.

## Partial Confidence Scans

Some repeated observations may fail. Example: 300 planned, 270 succeed,
30 fail. Scan status: `PARTIAL`. Used AI Checks: 270. Released: 30.
Reliability calculations use 270 successful observations with 90%
measurement coverage.

## Analysis

Existing deterministic Phase 7 analysis analyzes all SUCCEEDED repeated
PromptRuns exactly as normal evidence. No new LLM analysis. No extra
quota. One EntityMention set per actual PromptRun.

## Reliability Metrics

### Measurement Cell

A Confidence measurement cell is `Prompt ID x Provider` within the
Confidence Scan. Each cell plans `repeat_count` observations.
`attempt_number` is NOT part of the logical cell identity.

### Per-Cell Entity Observations

For each cell x entity, from SUCCEEDED repeats:

- `successful_repeats`
- `mentioned_repeats`
- `mention_frequency = mentioned_repeats / successful_repeats x 100`
- If `successful_repeats == 0`: `mention_frequency = NULL`

### Repeat-Analyzable Cell

A cell is repeat-analyzable when `successful_repeats >= 2`. A cell with
only one successful observation cannot establish repeat stability.

### Stable Cell

For a given entity, a repeat-analyzable cell is stable if all successful
repeats agree: `mentioned_repeats == 0` OR `mentioned_repeats == successful_repeats`.
Both "always appears" and "consistently does not appear" are stable.

### Variable Cell

For a repeat-analyzable cell: `0 < mentioned_repeats < successful_repeats`.

### Repeat Sufficiency

`Repeat Sufficiency = cells with >= 2 successful repeats / all planned cells x 100`

### Mention Stability

`Mention Stability = stable repeat-analyzable cells / all repeat-analyzable cells x 100`

If zero repeat-analyzable cells: NULL.

This is NOT the same as visibility. Example: brand absent in every
repeat -> Visibility = 0%, Mention Stability = 100%. The measured
absence was stable.

### Round Visibility

For each `observation_index`, ordinary Phase 7 visibility is calculated
using only SUCCEEDED runs in that round. If a round has zero successful
observations: visibility = NULL.

### Observed Visibility Range

For each entity, collect non-NULL round visibility rates:

- `observed_visibility_min`
- `observed_visibility_max`
- `observed_visibility_range = max - min`

This is NOT a statistical confidence interval. It is simply the observed
range across repeated measurement rounds.

### Failed Runs

FAILED repeats:
- Do NOT count as "NO mention"
- Contribute only to: planned observations, coverage loss, repeat
  sufficiency loss
- Do NOT enter: visibility denominator, mention presence/absence
  denominator

### True Zero

If all successful repeated observations exist and brand never appears:
visibility = 0%. If every repeat cell consistently says no: mention
stability may be 100%. This is a strong, stable measured zero. Do NOT
convert it to NULL.

### No Measurement

If zero successful observations: visibility = NULL, confidence level =
INSUFFICIENT. No fake zero.

## Measurement Confidence Level

A transparent heuristic. This is a PRODUCT EVIDENCE-QUALITY LABEL.

It is NOT:
- Statistical confidence
- Probability of truth
- P-value
- 95% CI

### Values

- `INSUFFICIENT`
- `LOW`
- `MEDIUM`
- `HIGH`

### v1 Thresholds

**INSUFFICIENT** when ANY:
- Fewer than 2 rounds have valid successful observations
- Measurement coverage < 50%
- Repeat sufficiency < 50%

**LOW** when not INSUFFICIENT and ANY:
- Measurement coverage < 75%
- Repeat sufficiency < 75%
- Mention stability is not NULL and < 60%

**HIGH** only when ALL:
- `repeat_count >= 5`
- Measurement coverage >= 90%
- Repeat sufficiency >= 90%
- Mention stability >= 80%

**Otherwise**: MEDIUM

### Default Three Repeats

With default `repeat_count=3`, a scan can reach at most MEDIUM under
v1. HIGH requires at least 5 repeats. Do not label three repeated
observations as extremely high statistical certainty.

### Methodology Version

`CONFIDENCE_METHODOLOGY_VERSION = "repeat-reliability-v1"`

Returned in API responses. Thresholds may evolve with a version bump.

## API

### Create Confidence Scan

```
POST /api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{baseline_scan_id}/confidence
```

Requires `Idempotency-Key` header.

Body:
```json
{
  "repeat_count": 3  // optional, default=3, min=2, max=5
}
```

Response: `202 Accepted`
```json
{
  "scan_id": "...",
  "baseline_scan_id": "...",
  "scan_type": "CONFIDENCE",
  "repeat_count": 3,
  "prompt_count": 25,
  "provider_count": 4,
  "planned_ai_checks": 300,
  "status": "PENDING"
}
```

### Get Confidence Results

```
GET /api/v1/workspaces/{workspace_id}/projects/{project_id}/scans/{scan_id}/confidence
```

Query params:
- `prompt_type` (default: `NON_BRANDED`)
- `provider` (optional filter)

Only valid for `ScanType.CONFIDENCE`.

Returns: `confidence_methodology_version`, `baseline_scan_id`,
`repeat_count`, scope, planned/successful observations, measurement
coverage, round summaries, entity reliability, provider breakdown.

### Role Matrix

- OWNER: create/read
- ADMIN: create/read
- MEMBER: read only (cannot initiate extra paid calls)

### Tenant Isolation

Workspace A cannot create Confidence from Workspace B baseline or read
Workspace B confidence results. Foreign baseline scan: 404. Foreign
confidence scan: 404.

## Idempotency

Confidence endpoint requires `Idempotency-Key`. Same workspace +
baseline scan + repeat_count + same key -> same Confidence Scan, no
duplicate quota reservation, no duplicate dispatch. Same key but
different baseline_scan_id or repeat_count -> ConflictError.

## No Statistical Claims

Do NOT calculate or market:
- 95% confidence interval
- Statistical significance
- P-values
- Standard error implying random iid sampling
- Causal certainty

Use: observed range, coverage, repeat stability, heuristic confidence
level.

## No Response-Semantic Similarity

Phase 8 does NOT implement embedding similarity, BLEU, semantic answer
similarity, LLM comparison, or sentiment consistency. Confidence is
about repeatability of entity presence, visibility, and citation
evidence using existing deterministic evidence.

## Phase 8.1: Metrics Integrity and Analysis Readiness

### Analysis Readiness

Confidence metrics and visibility metrics require a `COMPLETED`
`ScanAnalysis` before computing any mention-based metrics.

A missing, `PENDING`, `RUNNING`, or `FAILED` analysis is NOT evidence
that a brand was absent. The services fail closed with:

```
ConflictError("Scan analysis is not completed.")
```

The services do NOT silently populate zero-valued mention metrics when
analysis is unavailable.

### True Zero vs No Measurement

- **True measured zero**: `COMPLETED` analysis + successful observations
  exist + brand detected in zero of them → `visibility_rate = 0%`.
  Mention stability may be 100% because the measured absence was stable.
- **No measurement**: zero successful observations →
  `visibility = NULL`, `confidence = INSUFFICIENT`. No fake zero.
- **Analysis not ready**: missing/failed analysis → `ConflictError`.
  No fake zero.

### Provider Isolation

Every `ProviderReliability` value is calculated ONLY from that
provider's `PromptRuns`:

- **Brand visibility numerator**: SUCCEEDED runs from provider P that
  mention BRAND, intersected with P's successful run IDs. Never divides
  mentions from all providers by successes from one provider.
- **Round summaries**: provider-specific. A round is valid for provider
  P only if P has successful observations in that round. Provider P
  does NOT inherit another provider's valid rounds.
- **Visibility range**: `observed_visibility_min` and
  `observed_visibility_max` come only from P's round visibility. No
  overall multi-provider range for individual providers.
- **Confidence level**: uses provider-specific measurement coverage,
  valid round count, repeat sufficiency, and BRAND mention stability.
- **Mention stability**: calculated from P's Prompt x Provider cells
  only. Competitors or another provider's runs do not influence P's
  stability.

### Brand Snapshot Identification

Provider breakdown identifies the primary tracked brand using
`TrackedEntityType.BRAND` (the approved Phase 7 enum). The legacy
`"PRIMARY_BRAND"` string is not used. Comparison is robust to
SQLAlchemy returning either the enum object or the string-backed value.

### Failed Provider Observations

FAILED provider observations:
- Reduce that provider's measurement coverage
- Do NOT count as brand absence (no fake 0% visibility)
- Do NOT inherit another provider's successful rounds or visibility range
- Result in `brand_visibility_rate = NULL` when the provider has zero
  successful observations

### Provider Enum Normalization

`ProviderReliability.provider` is safely normalized using
`LLMProvider(value)` to handle SQLAlchemy string-backed enum fields that
may return plain strings. This prevents
`AttributeError: 'str' object has no attribute 'value'`.
